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
    # struct proc_bsdinfo (136 bytes): status@4, pid@12, ppid@16, uid@20, pgid@100.
    info = ctypes.create_string_buffer(136)
    table = {}
    for pid in pids[:max(count, 0)]:
        if pid <= 0 or lib.proc_pidinfo(pid, 3, ctypes.c_uint64(0), info, 136) != 136:  # PROC_PIDTBSDINFO
            continue
        status, = struct.unpack_from("I", info.raw, 4)
        if status == 5:  # SZOMB
            continue
        ppid, uid = struct.unpack_from("II", info.raw, 16)
        pgid, = struct.unpack_from("I", info.raw, 100)
        table[pid] = ProcessInfo(pid, ppid, pgid, uid, None)
    return table


def _darwin_argv(pid: int) -> tuple[str, ...]:
    libc = _libproc()  # libproc re-exports libSystem's sysctl.
    if not libc:
        return ()
    global _ARGS_BUFFER
    if _ARGS_BUFFER is None:
        _ARGS_BUFFER = ctypes.create_string_buffer(1 << 20)  # >= KERN_ARGMAX
    buffer = _ARGS_BUFFER
    size = ctypes.c_size_t(len(buffer))
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0 or size.value < 4:
        return ()
    argc, = struct.unpack_from("i", buffer.raw, 0)
    rest = buffer.raw[4:size.value]
    # Executable path, NUL padding, then argc arguments.
    _, _, rest = rest.partition(b"\0")
    rest = rest.lstrip(b"\0")
    return tuple(os.fsdecode(arg) for arg in rest.split(b"\0")[:argc])


def process_argv(info: ProcessInfo) -> tuple[str, ...]:
    if info.argv is not None:
        return info.argv
    return _darwin_argv(info.pid) if sys.platform == "darwin" else ()


def _own_lineage() -> set[int]:
    """This process and its ancestors are never stop targets."""
    table = process_table()
    lineage = {os.getpid()}
    pid = os.getppid()
    while pid > 1 and pid not in lineage:
        lineage.add(pid)
        pid = table[pid].ppid if pid in table else 0
    return lineage


def descendants(table: dict[int, ProcessInfo], roots: set[int]) -> set[int]:
    """All processes reachable through parent links, whatever group or session they joined."""
    children: dict[int, list[int]] = {}
    for info in table.values():
        children.setdefault(info.ppid, []).append(info.pid)
    found: set[int] = set()
    pending = list(roots)
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _escaped_tree(pgid: int) -> set[int]:
    """Descendants of a process group that moved to another group or session."""
    table = process_table()
    members = {info.pid for info in table.values() if info.pgid == pgid} | {pgid}
    uid = os.getuid()
    return {
        pid for pid in descendants(table, members)
        if table[pid].pgid != pgid and table[pid].uid == uid
    } - _own_lineage()


def _signal_each(pids: set[int], sig: int) -> set[int]:
    alive = set()
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue  # Gone, or not ours to stop.
        alive.add(pid)
    return alive


def _kill_escaped(pids: set[int]) -> None:
    if not pids:
        return
    # Include anything they spawned while the group was being stopped.
    _signal_each((pids | descendants(process_table(), pids)) - _own_lineage(), signal.SIGKILL)


def kill_process_group(pid: int) -> None:
    # Take the snapshot before signalling: once a parent exits, its escaped
    # descendants are reparented and no longer traceable to this run.
    # Freeze them so their exit cannot look like a normal executor finish
    # while the runner is still shutting down.
    escaped = _escaped_tree(pid)
    _signal_each(escaped, signal.SIGSTOP)
    try:
        _kill_group(pid)
    finally:
        _kill_escaped(escaped)


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
    skip = set(protected) | descendants(table, set(protected)) | _own_lineage()
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
            start_new_session=True, env=_child_environment(cwd),
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
