"""Bounded subprocesses and process group termination."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid


_process_group_callback: ContextVar[Callable[[int | None], None] | None] = ContextVar(
    "process_group_callback", default=None,
)


@contextmanager
def track_process_group(callback: Callable[[int | None], None]) -> Iterator[None]:
    """Observe subprocess ownership within one executor call without changing its API."""
    token = _process_group_callback.set(callback)
    try:
        yield
    finally:
        _process_group_callback.reset(token)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    pgid: int
    uid: int
    argv: tuple[str, ...] | None  # None: read lazily with process_argv().
    # macOS never reuses these and keeps the original parent's id after reparenting.
    unique: int | None = None
    parent_unique: int | None = None


def process_table() -> dict[int, ProcessInfo]:
    """Snapshot live processes without starting a subprocess; unknown means empty."""
    if sys.platform == "darwin":
        return _darwin_table()
    if os.path.isdir("/proc/self"):
        return _proc_table()
    return {}


def _proc_table() -> dict[int, ProcessInfo]:
    table = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            fields = stat.rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                continue  # Exited; nothing left to stop.
            uid = os.stat(f"/proc/{entry}").st_uid
            raw = Path(f"/proc/{entry}/cmdline").read_bytes()
        except (OSError, IndexError):
            continue
        argv = tuple(os.fsdecode(arg) for arg in raw.split(b"\0") if arg)
        table[int(entry)] = ProcessInfo(int(entry), int(fields[1]), int(fields[2]), uid, argv)
    return table


_LIBPROC = None
_ARGS_BUFFER = None


def _libproc():
    global _LIBPROC
    if _LIBPROC is None:
        try:
            _LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        except OSError:
            _LIBPROC = False
    return _LIBPROC


def _darwin_table() -> dict[int, ProcessInfo]:
    lib = _libproc()
    if not lib:
        return {}
    count = lib.proc_listallpids(None, 0)
    if count <= 0:
        return {}
    pids = (ctypes.c_int * (count * 2))()
    count = lib.proc_listallpids(pids, ctypes.sizeof(pids))
    # struct proc_bsdinfowithuniqid (192 bytes): proc_bsdinfo with status@4, ppid@16,
    # uid@20, pgid@100, then proc_uniqidentifierinfo with p_uniqueid@152, p_puniqueid@160.
    size = 192
    info = ctypes.create_string_buffer(size)
    table = {}
    for pid in pids[:max(count, 0)]:
        if pid <= 0 or lib.proc_pidinfo(pid, 18, ctypes.c_uint64(0), info, size) != size:
            continue  # PROC_PIDT_BSDINFOWITHUNIQID; unreadable processes are not ours.
        raw = info.raw
        status, = struct.unpack_from("I", raw, 4)
        if status == 5:  # SZOMB
            continue
        ppid, uid = struct.unpack_from("II", raw, 16)
        pgid, = struct.unpack_from("I", raw, 100)
        unique, parent_unique = struct.unpack_from("QQ", raw, 152)
        table[pid] = ProcessInfo(pid, ppid, pgid, uid, None, unique, parent_unique)
    return table


def _darwin_procargs(pid: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (argv, environment) of a process, or empty tuples when unreadable."""
    libc = _libproc()  # libproc re-exports libSystem's sysctl.
    if not libc:
        return (), ()
    global _ARGS_BUFFER
    if _ARGS_BUFFER is None:
        _ARGS_BUFFER = ctypes.create_string_buffer(1 << 20)  # >= KERN_ARGMAX
    buffer = _ARGS_BUFFER
    size = ctypes.c_size_t(len(buffer))
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0 or size.value < 4:
        return (), ()
    raw = ctypes.string_at(buffer, size.value)
    argc, = struct.unpack_from("i", raw, 0)
    # Executable path, NUL padding, argc arguments, then the environment.
    _, _, rest = raw[4:].partition(b"\0")
    strings = rest.lstrip(b"\0").split(b"\0")
    environment = []
    for item in strings[argc:]:
        if not item:
            break
        environment.append(os.fsdecode(item))
    return tuple(os.fsdecode(arg) for arg in strings[:argc]), tuple(environment)


def process_argv(info: ProcessInfo) -> tuple[str, ...]:
    if info.argv is not None:
        return info.argv
    return _darwin_procargs(info.pid)[0] if sys.platform == "darwin" else ()


# Inherited by every process an executor starts, even after setsid or after its
# parent exits, so a stop can find descendants that parent links no longer show.
TREE_MARK = "FORAGENT_PROCESS_TREE"


def process_tree_mark(pid: int) -> str | None:
    if sys.platform == "darwin":
        environment = _darwin_procargs(pid)[1]
    else:
        try:
            environment = tuple(
                os.fsdecode(item) for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            )
        except OSError:
            return None
    prefix = TREE_MARK + "="
    return next((item[len(prefix):] for item in environment if item.startswith(prefix)), None) or None


def _own_lineage() -> set[int]:
    """This process and its ancestors are never stop targets."""
    table = process_table()
    lineage = {os.getpid()}
    pid = os.getppid()
    while pid > 1 and pid not in lineage:
        lineage.add(pid)
        pid = table[pid].ppid if pid in table else 0
    return lineage


def descendants(table: dict[int, ProcessInfo], roots: set[int], exclude: set[int] = frozenset()) -> set[int]:
    """All processes reachable through parent links, whatever group or session they joined.

    Excluded processes are neither returned nor walked through.
    """
    children: dict[int, list[int]] = {}
    for info in table.values():
        children.setdefault(info.ppid, []).append(info.pid)
    found: set[int] = set()
    pending = list(set(roots) - exclude)
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found and child not in exclude:
                found.add(child)
                pending.append(child)
    return found


@dataclass(frozen=True)
class TreeSnapshot:
    escaped: set[int]  # Descendants that left the group.
    uniques: set[int]  # macOS unique ids of the whole tree.
    marks: set[str]  # Tree marks the whole tree carries.


def _is_gate(info: ProcessInfo) -> bool:
    """A gate tick or runner is a separate job, e.g. the tick a stopping runner kicks."""
    argv = process_argv(info)
    return any(argv[i] == "gate" and argv[i + 1] in {"tick", "run-issue"} for i in range(len(argv) - 1))


def _later_tree(table: dict[int, ProcessInfo], snapshot: TreeSnapshot, lineage: set[int]) -> set[int]:
    """Processes started by the tree after the snapshot, even if their parent already exited."""
    uid = os.getuid()
    known = set(snapshot.uniques)
    blocked = {table[pid].unique for pid in lineage if pid in table} - {None}
    found: set[int] = set()
    changed = True
    while changed:  # Follow original-parent links through processes that are still alive.
        changed = False
        for info in table.values():
            if info.pid in found or info.pid in lineage or info.uid != uid or info.unique in blocked:
                continue
            if info.unique in known or info.parent_unique in known:
                if _is_gate(info):
                    blocked.add(info.unique)
                    continue
                found.add(info.pid)
                known.add(info.unique)
                changed = True
    if snapshot.marks:
        found |= {
            info.pid for info in table.values()
            if info.uid == uid and info.pid not in lineage
            and process_tree_mark(info.pid) in snapshot.marks and not _is_gate(info)
        }
    return found


def _snapshot_tree(pgid: int) -> TreeSnapshot:
    table = process_table()
    lineage = _own_lineage()
    members = {info.pid for info in table.values() if info.pgid == pgid} | {pgid}
    tree = descendants(table, members, exclude=lineage)
    gates = {pid for pid in tree - members if _is_gate(table[pid])}
    if gates:
        tree = descendants(table, members, exclude=lineage | gates)
    uid = os.getuid()
    escaped = {pid for pid in tree if table[pid].pgid != pgid and table[pid].uid == uid}
    everyone = (members | tree) - lineage
    uniques = {table[pid].unique for pid in everyone if pid in table} - {None}
    marks: set[str] = set()
    if not sys.platform == "darwin":  # macOS hides platform binaries' environments.
        own_marks = {process_tree_mark(pid) for pid in lineage}
        marks = {process_tree_mark(pid) for pid in everyone} - own_marks - {None}
    return TreeSnapshot(escaped, uniques, marks)


def _signal_each(pids: set[int], sig: int) -> set[int]:
    alive = set()
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue  # Gone, or not ours to stop.
        alive.add(pid)
    return alive


def _kill_escaped(snapshot: TreeSnapshot) -> None:
    if not (snapshot.escaped or snapshot.uniques or snapshot.marks):
        return
    table = process_table()
    lineage = _own_lineage()
    targets = (snapshot.escaped - lineage) | _later_tree(table, snapshot, lineage)
    gates = {info.pid for info in table.values() if info.ppid in targets and _is_gate(info)}
    _signal_each(targets | descendants(table, targets, exclude=lineage | gates), signal.SIGKILL)


def kill_process_group(pid: int) -> None:
    # Take the snapshot before signalling: once a parent exits, its escaped
    # descendants are reparented and parent links no longer show them.
    # Freeze them so their exit cannot look like a normal executor finish
    # while the runner is still shutting down.
    snapshot = _snapshot_tree(pid)
    _signal_each(snapshot.escaped, signal.SIGSTOP)
    try:
        _kill_group(pid)
    finally:
        _kill_escaped(snapshot)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(10)
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.killpg(pid, 0)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        pass  # macOS reports a group with only exiting members as EPERM; confirm below.
    # SIGKILL delivery is asynchronous. Reap a direct child where possible and
    # confirm exit before tick removes files the runner could still be using.
    for _ in range(50):
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass  # Still exiting; keep waiting until the group is gone.
        time.sleep(0.1)
    raise TimeoutError(f"Runner process group {pid} did not terminate")


def _process_cwd(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
    if sys.platform == "darwin":
        return _darwin_cwd(pid)
    return None


def _darwin_cwd(pid: int) -> str | None:
    lib = _libproc()
    if not lib:
        return None
    # struct proc_vnodepathinfo: vnode_info (152 bytes) + path[MAXPATHLEN] for cwd, then root.
    size = 2 * (152 + 1024)
    buffer = ctypes.create_string_buffer(size)
    if lib.proc_pidinfo(pid, 9, ctypes.c_uint64(0), buffer, size) != size:  # PROC_PIDVNODEPATHINFO
        return None
    path = buffer.raw[152:152 + 1024].split(b"\0", 1)[0]
    return os.fsdecode(path) or None


def _forms(path: Path) -> set[str]:
    return {str(path), os.path.realpath(path)}


def _inside(candidate: str, roots: set[str]) -> bool:
    return any(candidate == root or candidate.startswith(root.rstrip("/") + "/") for root in roots)


def worktree_processes(
    worktrees: dict[int, Path], *, protected: set[int] = frozenset(),
) -> dict[int, set[int]]:
    """Processes of this user whose cwd or command line lies inside each worktree."""
    if not worktrees:
        return {}
    table = process_table()
    lineage = _own_lineage()
    skip = set(protected) | descendants(table, set(protected), exclude=lineage) | lineage
    uid = os.getuid()
    forms = {number: _forms(path) for number, path in worktrees.items()}
    patterns = {
        number: re.compile(
            r"(?:^|[\s=:'\"])(?:" + "|".join(re.escape(form.rstrip("/")) for form in sorted(paths))
            + r")(?=$|[/\s'\":])"
        )
        for number, paths in forms.items()
    }
    found: dict[int, set[int]] = {}
    for info in table.values():
        if info.uid != uid or info.pid in skip or info.pid <= 1:
            continue
        cwd = _process_cwd(info.pid) or ""
        number = next((number for number, paths in forms.items() if cwd and _inside(cwd, paths)), None)
        if number is None:
            command = " ".join(process_argv(info))
            number = next((number for number in forms if patterns[number].search(command)), None)
        if number is not None:
            found.setdefault(number, set()).add(info.pid)
    return found


def reap_worktree_processes(
    worktrees: dict[int, Path], *, protected: set[int] = frozenset(),
) -> dict[int, set[int]]:
    """Stop processes left inside worktrees and return the stopped PIDs per issue."""
    found = worktree_processes(worktrees, protected=protected)
    signalled = _signal_each(set().union(*found.values()) if found else set(), signal.SIGTERM)
    alive = signalled
    for _ in range(50):
        alive = {pid for pid in alive if _exists(pid)}
        if not alive:
            break
        time.sleep(0.1)
    _signal_each(alive, signal.SIGKILL)
    return {number: pids & signalled for number, pids in found.items() if pids & signalled}


def _exists(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith("linux"):
        try:  # A reparented zombie has exited even if nobody reaped it yet.
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    return True


def _child_environment(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
        env.pop(key, None)
    gate_bins = {(Path(__file__).resolve().parents[2] / ".venv/bin").resolve()}
    if sys.prefix != sys.base_prefix:
        gate_bins.add((Path(sys.prefix) / "bin").resolve())
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(
            entry for entry in env["PATH"].split(os.pathsep)
            if not entry or (cwd / entry).resolve() not in gate_bins
        )
    return env


def run_logged(
    argv: list[str], *, cwd: Path, timeout_s: int, log_path: Path,
    prompt: str | None = None, capture_stdout: bool = False,
    stdout_path: Path | None = None, terminate_children: bool = False,
    stderr_path: Path | None = None,
) -> tuple[int | None, bool, str]:
    """Append output without buffering an unbounded session in memory."""
    on_process_group = _process_group_callback.get()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log, (
        stdout_path.open("w+b") if stdout_path is not None else tempfile.TemporaryFile()
    ) as output, (stderr_path.open("ab") if stderr_path is not None else nullcontext()) as errors:
        process = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
            stdout=output if capture_stdout else log,
            stderr=errors if errors is not None else log if capture_stdout else subprocess.STDOUT,
            start_new_session=True, env={**_child_environment(cwd), TREE_MARK: uuid.uuid4().hex},
        )
        # Executors have their own session, so relay tick termination to them.
        previous = None
        can_handle = threading.current_thread() is threading.main_thread()

        def terminated(signum, frame):
            raise SystemExit(128 + signum)

        if can_handle:
            previous = signal.signal(signal.SIGTERM, terminated)
        timed_out = False
        forwarded = 0
        stop_forwarding = threading.Event()

        def forward_stdout():
            nonlocal forwarded
            output.flush()
            while chunk := os.pread(output.fileno(), 65536, forwarded):
                log.write(chunk)
                log.flush()
                forwarded += len(chunk)

        def follow_stdout():
            while not stop_forwarding.wait(1):
                forward_stdout()

        follower = threading.Thread(target=follow_stdout, daemon=True) if capture_stdout else None
        if follower is not None:
            follower.start()
        try:
            if on_process_group is not None:
                on_process_group(process.pid)  # start_new_session makes PID == PGID.
            try:
                process.communicate(input=prompt.encode("utf-8") if prompt is not None else None, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
        finally:
            try:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                if terminate_children:
                    kill_process_group(process.pid)
                if on_process_group is not None:
                    on_process_group(None)
            finally:
                if can_handle:
                    signal.signal(signal.SIGTERM, previous)
                if follower is not None:
                    stop_forwarding.set()
                    follower.join()
                    forward_stdout()
        if capture_stdout:
            size = output.seek(0, 2)
            output.seek(max(0, size - 16000))
            tail = output.read().decode("utf-8", errors="replace")[-4000:]
        else:
            tail = ""
    return process.returncode, timed_out, tail
