"""Bounded subprocesses and termination of recorded process trees."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field

import errno
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

import psutil

from foragent.records import load_record


# Each observer accepts scans started at or after its previous poll's start.
# A child alive then and still alive at the scan is witnessed; discovery takes
# at most two poll intervals (in addition to time spent waiting/scanning).
OBSERVER_POLL_INTERVAL = 1.0
# SIGTERM relay joins the observer: its scan work can block for at most this
# lock bound plus one scan pair, including when contention requires fallback.
ROUTINE_SCAN_LOCK_TIMEOUT = 2.0
ROUTINE_SCAN_LOCK_RETRY_INTERVAL = 0.01


_process_group_callback: ContextVar[Callable[[int | None], None] | None] = ContextVar(
    "process_group_callback", default=None,
)


_process_tree_callback: ContextVar[Callable[[list[dict]], None] | None] = ContextVar(
    "process_tree_callback", default=None,
)


@contextmanager
def track_process_group(
    callback: Callable[[int | None], None], *,
    tree_callback: Callable[[list[dict]], None] | None = None,
) -> Iterator[None]:
    """Observe subprocess ownership within one executor call without changing its API."""
    token = _process_group_callback.set(callback)
    tree_token = _process_tree_callback.set(tree_callback)
    try:
        yield
    finally:
        _process_tree_callback.reset(tree_token)
        _process_group_callback.reset(token)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    pgid: int
    sid: int
    start_time: float
    argv: tuple[str, ...] | None = ()
    uid: int | None = None


class ProcessTable(tuple):
    """A listing with explicitly unreadable PIDs, which cannot prove death."""

    def __new__(cls, rows=(), unreadable=(), unreadable_uids=()):
        table = super().__new__(cls, rows)
        table.unreadable = frozenset(unreadable)
        table.unreadable_uids = dict(unreadable_uids)
        return table


@dataclass
class ProcessSnapshot:
    rows: dict[int, ProcessIdentity]
    unreadable: frozenset[int] = frozenset()
    available: bool = True
    captured_at: float = 0
    unreadable_uids: dict[int, int] = field(default_factory=dict)

    def runner_exists(self, number: int) -> bool:
        return runner_commands_exist(number, (
            " ".join(row.argv) for row in self.rows.values() if row.argv is not None
        ))


def runner_commands_exist(number: int, commands) -> bool:
    pattern = re.compile(
        rf"(?:^|\s)(?:\S*/)?(?:foragent|fa|foragent\.cli)\s+gate\s+run-issue\s+{number}(?=\s|$)"
    )
    return any(command is not None and pattern.search(command) is not None for command in commands)


_snapshot: ContextVar[ProcessSnapshot | None] = ContextVar("process_snapshot", default=None)


def current_snapshot() -> ProcessSnapshot | None:
    return _snapshot.get()


def _capture_process_snapshot() -> ProcessSnapshot:
    captured_at = time.time()
    try:
        table = list_processes()
        return ProcessSnapshot({row.pid: row for row in table}, getattr(table, "unreadable", frozenset()),
                               captured_at=captured_at,
                               unreadable_uids=getattr(table, "unreadable_uids", {}))
    except OSError as exc:
        logging.getLogger(__name__).warning("Process snapshot unavailable; liveness unknown; skipping prune: %s", exc)
        return ProcessSnapshot({}, available=False)


@contextmanager
def process_snapshot():
    """Capture routine positive liveness and prune candidates for one tick."""
    snapshot = _capture_process_snapshot()
    token = _snapshot.set(snapshot)
    try:
        yield snapshot
    finally:
        _snapshot.reset(token)


def read_process_identity(pid: int) -> ProcessIdentity | None:
    """Return a verified live kernel identity or None; read errors propagate."""
    try:
        process = psutil.Process(pid)
        start = process.create_time()
        row = ProcessIdentity(pid, process.ppid(), os.getpgid(pid), os.getsid(pid), start)
        if psutil.Process(pid).create_time() == start and process.status() != psutil.STATUS_ZOMBIE:
            return row
    except (psutil.NoSuchProcess, ProcessLookupError):
        pass
    except psutil.AccessDenied as exc:
        raise PermissionError(f"Process identity {pid} cannot be read") from exc
    return None


def _same_dir(cwd: str | None, root: Path) -> bool:
    """``cwd`` is ``root``. A missing, empty or relative path matches nothing:
    resolving it would silently use this process's own directory instead."""
    if not cwd or not Path(cwd).is_absolute():
        return False
    try:
        return Path(cwd).resolve() == root.resolve()
    except OSError:
        return False


def _cwd(process) -> str | None:
    """Absolute working directory, or None when it is unreadable, empty or gone."""
    try:
        cwd = process.cwd()
    except (psutil.Error, OSError, SystemError):
        return None
    return cwd if isinstance(cwd, str) and cwd and Path(cwd).is_absolute() else None


def claude_session() -> dict | None:
    """``{pid, start_time}`` of the nearest Claude ancestor of this process, or None.

    A Claude Code session shows argv[0] ``claude`` and psutil name ``claude`` or
    ``claude.exe``. The nearest such ancestor is the session, wherever it runs
    and whatever lies between.
    """
    try:
        current = psutil.Process(os.getpid()).parent()
        while current is not None:
            try:
                argv = current.cmdline()
            except (psutil.AccessDenied, PermissionError, SystemError):
                argv = []
            try:
                name = current.name()
            except (psutil.AccessDenied, PermissionError, SystemError):
                name = ""
            if (argv and Path(argv[0]).name == "claude") or name in {"claude", "claude.exe"}:
                return {"pid": current.pid, "start_time": current.create_time()}
            current = current.parent()
    except (psutil.Error, OSError, SystemError):
        return None
    return None


def orchestrator_process_alive(repo_root: Path, skill: str = "/fa-orchestrator") -> bool:
    """True when a live process has ``skill`` in its argv and runs at ``repo_root``.

    The working directory is read only for those candidates; an unreadable or
    missing one does not match.
    """
    try:
        candidates = tuple(psutil.process_iter())
    except (psutil.Error, OSError):
        return False
    for process in candidates:
        try:
            argv = process.cmdline()
        except (psutil.Error, OSError, SystemError):
            continue
        if skill in (argv or ()) and _same_dir(_cwd(process), repo_root):
            return True
    return False


@dataclass(frozen=True)
class IdentityEvidence:
    alive: bool | None  # None means unreadable, never permission to discard ownership.
    row: ProcessIdentity | None = None


def recorded_identity_evidence(
    pid: int, start_time: float, *, rows=None, fresh: bool = False,
) -> IdentityEvidence:
    """Only a fresh PID read can prove a recorded lifetime dead or replaced.

    A matching listing is positive evidence. Missing, unreadable and mismatched
    cached rows all require a fresh read before pruning. Stop and cleanup paths
    use their own fresh listings, independently of this routine evidence.
    """
    if not fresh:
        if rows is None:
            snapshot = current_snapshot()
            rows = snapshot.rows if snapshot is not None else {}
        row = rows.get(pid)
        if row is not None and row.start_time == start_time:
            return IdentityEvidence(True, row)
    try:
        row = read_process_identity(pid)
    except Exception:
        return IdentityEvidence(None)
    if row is not None and row.pid == pid and row.start_time == start_time:
        return IdentityEvidence(True, row)
    return IdentityEvidence(False)


def _caused_by_permission_error(error: BaseException) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, (psutil.AccessDenied, PermissionError)):
            return True
        seen.add(id(error))
        error = error.__cause__
    return False


def list_processes() -> tuple[ProcessIdentity, ...]:
    """Read kernel identities without probing or signaling any process."""
    rows, unreadable, unreadable_uids = [], set(), {}
    try:
        candidates = tuple(psutil.process_iter())
    except (psutil.AccessDenied, PermissionError) as exc:
        raise PermissionError("Process table unavailable; ownership cannot be verified") from exc
    for process in candidates:
        uid = None
        try:
            try:
                uid = process.uids().real
            except (psutil.AccessDenied, PermissionError):
                pass
            # A second creation-time read rejects a PID reused during enumeration.
            start = process.create_time()
            try:
                argv = tuple(process.cmdline())
            except (psutil.AccessDenied, PermissionError):
                argv = None  # Readable identity, but runner discovery remains unknown.
            except SystemError as exc:
                # macOS psutil can wrap a kernel permission error in SystemError.
                if not _caused_by_permission_error(exc):
                    raise
                argv = None
            row = ProcessIdentity(process.pid, process.ppid(), os.getpgid(process.pid),
                                  os.getsid(process.pid), start, argv, uid)
            if psutil.Process(process.pid).create_time() == start and process.status() != psutil.STATUS_ZOMBIE:
                rows.append(row)
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except (psutil.Error, OSError, SystemError):
            unreadable.add(process.pid)
            if uid is not None:
                unreadable_uids[process.pid] = uid
    return ProcessTable(rows, unreadable, unreadable_uids)


def _protected(rows) -> tuple[set[int], int, int]:
    lineage = {1, os.getpid(), os.getppid()}
    current = os.getppid()
    while current in rows and rows[current].ppid not in lineage:
        current = rows[current].ppid
        lineage.add(current)
    return lineage, os.getpgrp(), os.getsid(0)


def observe_routine_tree(identities=()) -> list[dict]:
    """Retain ownership and add positively witnessed children during routine checks.

    This never removes identities or supplies a stop's descendant listing.
    """
    snapshot = current_snapshot()
    if snapshot is None:
        return observe_tree(identities)
    if not snapshot.available:
        return list(identities)
    rows = snapshot.rows
    lineage, group, session = _protected(rows)

    def safe(row):
        return (row.pid > 1 and row.pid not in lineage
                and row.pgid not in lineage and row.sid not in lineage
                and row.pgid != group and row.sid != session)

    known = {item["pid"]: item["start_time"] for item in identities}
    live = {pid for pid, start in known.items()
            if pid in rows and rows[pid].start_time == start and safe(rows[pid])}
    while True:
        children = {row.pid for row in rows.values() if row.pid not in known
                    and row.ppid in live and row.start_time >= rows[row.ppid].start_time and safe(row)}
        parents = {}
        for pid in {rows[pid].ppid for pid in children}:
            try:
                parents[pid] = read_process_identity(pid)
            except (OSError, psutil.Error):
                parents[pid] = None
        children = {pid for pid in children if parents[rows[pid].ppid] is not None
                    and (parents[rows[pid].ppid].start_time, parents[rows[pid].ppid].ppid,
                         parents[rows[pid].ppid].pgid, parents[rows[pid].ppid].sid)
                    == (rows[rows[pid].ppid].start_time, rows[rows[pid].ppid].ppid,
                        rows[rows[pid].ppid].pgid, rows[rows[pid].ppid].sid)}
        if not children:
            break
        known.update({pid: rows[pid].start_time for pid in children})
        live.update(children)
    return [{"pid": pid, "start_time": start} for pid, start in known.items()]


def _scan_tree_rows() -> tuple[dict[int, ProcessIdentity], dict[int, ProcessIdentity]]:
    observed = {row.pid: row for row in list_processes()}
    confirmed = {row.pid: row for row in list_processes()}
    # Enumeration is not atomic: a reused parent must not authorize its new
    # children from an earlier row in the same scan.
    rows = {pid: row for pid, row in observed.items() if pid in confirmed
            and (row.start_time, row.ppid) == (confirmed[pid].start_time, confirmed[pid].ppid)}
    return rows, confirmed


def _read_routine_rows(path: Path, not_before: float):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (type(value["version"]) is not int or value["version"] != 1
            or type(value["captured_at"]) not in (int, float)
            # Monotonic time is system-wide and immune to wall-clock steps.
            or not not_before <= value["captured_at"] <= time.monotonic()):
        return None

    def decode(records):
        if not isinstance(records, list):
            raise ValueError("Invalid process scan rows")
        rows = {}
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Invalid process scan identity")
            row = load_record(ProcessIdentity, record)
            if (any(type(getattr(row, name)) is not int for name in ("pid", "ppid", "pgid", "sid"))
                    or type(row.start_time) not in (int, float) or not math.isfinite(row.start_time)
                    or (row.uid is not None and type(row.uid) is not int)
                    or (row.argv is not None and (not isinstance(row.argv, list)
                                                 or any(not isinstance(arg, str) for arg in row.argv)))):
                raise ValueError("Invalid process scan identity")
            rows[row.pid] = ProcessIdentity(row.pid, row.ppid, row.pgid, row.sid, row.start_time,
                                            None if row.argv is None else tuple(row.argv), row.uid)
        return rows

    return decode(value["rows"]), decode(value["confirmed"])


@contextmanager
def _routine_scan_lock(path: Path) -> Iterator[None]:
    deadline = time.monotonic() + ROUTINE_SCAN_LOCK_TIMEOUT
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(ROUTINE_SCAN_LOCK_RETRY_INTERVAL, remaining))
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def shared_routine_rows(*, not_before: float) -> tuple[dict[int, ProcessIdentity], dict[int, ProcessIdentity]]:
    """Reuse a scan started since the caller's previous poll, never a future scan.

    Return reconciled rows and the complete confirmed rows for lineage checks.
    Cache failures use direct scans; stop and birth evidence never use this cache.
    """
    from foragent import capacity

    cache_errors = (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError)
    lock = ExitStack()
    try:
        try:
            path = capacity.machine_root() / "process-scan.json"
            cached = _read_routine_rows(path, not_before)
            if cached is not None:
                return cached
            lock.enter_context(_routine_scan_lock(path.with_suffix(".lock")))
            cached = _read_routine_rows(path, not_before)
        except cache_errors:
            path, cached = None, None
        if cached is not None:
            return cached

        # Enumeration errors belong to the observer, never to cache fallback.
        captured_at = time.monotonic()
        rows, confirmed = _scan_tree_rows()
        if path is None:
            return rows, confirmed
        temporary = None
        try:
            value = {"version": 1, "captured_at": captured_at}
            for name, table in (("rows", rows), ("confirmed", confirmed)):
                value[name] = [asdict(ProcessIdentity(
                    row.pid, row.ppid, row.pgid, row.sid, row.start_time, row.argv, getattr(row, "uid", None),
                )) for row in table.values()]
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream)
            os.replace(temporary, path)
        except cache_errors:
            pass  # The fresh rows remain usable when publication fails.
        finally:
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return rows, confirmed
    finally:
        try:
            lock.close()
        except cache_errors:
            pass


def observe_tree(
    identities=(), roots=(), *, shared: bool = False, not_before: float | None = None,
) -> list[dict]:
    """Keep observed identities across reparenting; only verified parents add children.

    roots are supplied only when Foragent records a newly launched process. A
    missing or reused recorded identity is never re-anchored by its PID alone.
    Only the periodic child observer opts into shared routine rows, supplying
    its previous poll's start as not_before. Other shared callers default to
    requiring a scan started since this observation.
    """
    if not identities and not roots:
        return []
    rows, confirmed = shared_routine_rows(
        not_before=time.monotonic() if not_before is None else not_before,
    ) if shared else _scan_tree_rows()
    lineage, group, session = _protected(confirmed)

    def safe(row):
        return (row.pid > 1 and row.pid not in lineage
                and row.pgid not in lineage and row.sid not in lineage
                and row.pgid != group and row.sid != session)

    fresh_rows = {}

    def fresh(row):
        if not shared:
            return True
        if row.pid not in fresh_rows:
            try:
                fresh_rows[row.pid] = read_process_identity(row.pid)
            except SystemError as exc:
                if not _caused_by_permission_error(exc):
                    raise
                fresh_rows[row.pid] = None
            except (OSError, psutil.Error):
                fresh_rows[row.pid] = None
        current = fresh_rows[row.pid]
        return (current is not None
                and (current.pid, current.start_time, current.ppid) == (row.pid, row.start_time, row.ppid))

    known = {}
    for item in identities:
        known.setdefault(item["pid"], item["start_time"])
    for pid in roots:
        # A runner can attest its own birth for a later gate to verify. It is
        # still excluded from traversal/signaling in its own current lineage.
        if pid in rows and (safe(rows[pid]) or pid == os.getpid()) and fresh(rows[pid]):
            known[pid] = rows[pid].start_time
    live = {pid for pid, start in known.items()
            if pid in rows and rows[pid].start_time == start and safe(rows[pid])}
    while True:
        children = {row.pid for row in rows.values() if row.pid not in known
                    and row.ppid in live and row.start_time >= rows[row.ppid].start_time
                    and safe(row) and fresh(row)}
        if not children:
            break
        for pid in children:
            known[pid] = rows[pid].start_time
        live.update(children)
    return [{"pid": pid, "start_time": start} for pid, start in known.items()]


_recorded_tree: ContextVar[list[dict] | None] = ContextVar("recorded_tree", default=None)


@contextmanager
def recorded_tree(identities):
    token = _recorded_tree.set(identities)
    try:
        yield
    finally:
        _recorded_tree.reset(token)


def kill_process_group(pid: int) -> int:
    """Stop a recorded tree by individual identities, never a numeric group.

    The historical name remains for callers. Each signal rechecks start time,
    parent, group and session against a fresh candidate snapshot. Linux pidfds
    bind the signal to that process lifetime. macOS has no equivalent: a
    microsecond exit/PID-reuse window remains between the final read and kill.
    Permission denial refuses the stop immediately, without another attempt.
    """
    recorded = _recorded_tree.get()
    if not recorded:
        raise ValueError(f"No recorded process identity for {pid}")
    identities = observe_tree(recorded)
    if not identities:
        return 0
    signaled = set()

    def current():
        rows = {row.pid: row for row in list_processes()}
        lineage, group, session = _protected(rows)
        return {item["pid"]: rows[item["pid"]] for item in identities
                if item["pid"] in rows and rows[item["pid"]].start_time == item["start_time"]
                and item["pid"] > 1 and item["pid"] not in lineage
                and rows[item["pid"]].pgid not in lineage and rows[item["pid"]].sid not in lineage
                and rows[item["pid"]].pgid != group and rows[item["pid"]].sid != session}

    def send(signum):
        nonlocal identities
        identities = observe_tree(identities)
        # Children first: preserve ownership before their parents can exit.
        for item in reversed(identities):
            target = item["pid"]
            candidate = current().get(target)
            if candidate is None:
                continue
            descriptor = None
            try:
                if callable(getattr(os, "pidfd_open", None)) and callable(getattr(signal, "pidfd_send_signal", None)):
                    try:
                        descriptor = os.pidfd_open(target, 0)
                    except OSError as exc:
                        if exc.errno != errno.ENOSYS:
                            raise
                # Open before verifying: a PID reused before pidfd_open must
                # not authorize the replacement's descriptor either.
                verified = read_process_identity(target)
                if verified is None or verified.pid != target or verified.start_time != candidate.start_time:
                    continue
                if (verified.ppid, verified.pgid, verified.sid) != (
                    candidate.ppid, candidate.pgid, candidate.sid,
                ):
                    raise PermissionError(f"Process identity changed before signaling {target}; not ours")
                if descriptor is not None:
                    signal.pidfd_send_signal(descriptor, signum)
                else:
                    os.kill(target, signum)
                signaled.add(target)
            except PermissionError:
                raise  # Not ours: do not escalate or retry another recorded root.
            except ProcessLookupError:
                continue
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    send(signal.SIGTERM)
    if current():
        time.sleep(10)
        send(signal.SIGKILL)
    for _ in range(50):
        for target in signaled:
            try:
                os.waitpid(target, os.WNOHANG)
            except ChildProcessError:
                pass
        if not current():
            return len(signaled)
        time.sleep(0.1)
    raise TimeoutError(f"Runner process tree {pid} did not terminate")


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
    stderr_path: Path | None = None, env: dict[str, str] | None = None,
) -> tuple[int | None, bool, str]:
    """Append output without buffering an unbounded session in memory; env adds variables."""
    on_process_group = _process_group_callback.get()
    on_process_tree = _process_tree_callback.get()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log, (
        stdout_path.open("w+b") if stdout_path is not None else tempfile.TemporaryFile()
    ) as output, (stderr_path.open("ab") if stderr_path is not None else nullcontext()) as errors:
        process = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
            stdout=output if capture_stdout else log,
            stderr=errors if errors is not None else log if capture_stdout else subprocess.STDOUT,
            # Closing stdin during cancellation must not wait for a buffered
            # writer's lock while a descendant keeps the pipe open.
            bufsize=0,
            start_new_session=True, env={**_child_environment(cwd), **(env or {})},
        )
        prompt_errors = []
        prompt_cancelled = False

        def write_prompt():
            try:
                pending = memoryview(prompt.encode("utf-8"))
                while pending:
                    written = process.stdin.write(pending)
                    pending = pending[written:]
            except (OSError, ValueError) as exc:
                prompt_errors.append(exc)
            finally:
                try:
                    process.stdin.close()
                except (OSError, ValueError) as exc:
                    prompt_errors.append(exc)

        writer = None
        if prompt is not None:
            # Deliver stdin before scanning, but leave all waiting/reaping to
            # the main thread so the launch snapshot can still record ownership.
            writer = threading.Thread(target=write_prompt, daemon=True)
            writer.start()
        launch_observed_at = time.monotonic()
        try:
            identities = observe_tree(roots=(process.pid,))
        except PermissionError:
            identities = []  # Normal completion is allowed; a stop must refuse.
        stop_tracking = threading.Event()

        observation_errors = []

        def observe_children():
            nonlocal identities
            previous_poll = launch_observed_at
            try:
                # The launch snapshot already anchors ownership. Keep witnessed
                # children across reparenting with shared machine-wide scan pairs.
                while not stop_tracking.wait(OBSERVER_POLL_INTERVAL):
                    poll_started_at = time.monotonic()
                    observed = observe_tree(identities, shared=True, not_before=previous_poll)
                    previous_poll = poll_started_at
                    if observed != identities:
                        identities = observed
                        if on_process_tree is not None:
                            on_process_tree(identities)
            except Exception as exc:
                observation_errors.append(exc)

        observer = threading.Thread(target=observe_children, daemon=True)
        observer.start()

        stop_failure = None

        def stop_children():
            nonlocal stop_failure
            # Timeout handling and finally share one failed termination. Cleanup
            # must propagate that failure, rather than start another stop.
            if stop_failure is not None:
                raise stop_failure
            try:
                if not identities:
                    if process.poll() is None:
                        raise PermissionError(f"Cannot verify executor identity {process.pid}")
                    return  # Completed without ownership evidence; do not infer children.
                with recorded_tree(identities):
                    kill_process_group(process.pid)
            except Exception as exc:
                stop_failure = exc
                raise

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
            if on_process_tree is not None:
                on_process_tree(identities)
            try:
                process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_children()
                process.wait()
            if observation_errors:
                raise observation_errors[0]
        finally:
            stop_tracking.set()
            # An in-flight observer waits at most the lock bound plus one scan
            # pair before this join lets cleanup relay SIGTERM to the executor.
            observer.join()
            try:
                identities = observe_tree(identities)
                if process.poll() is None:
                    stop_children()
                    process.wait()
                if terminate_children or observation_errors:
                    stop_children()
                if on_process_tree is not None:
                    on_process_tree(identities)
                if on_process_group is not None:
                    on_process_group(None)
            finally:
                if writer is not None:
                    # Root exit does not mean descendants have closed stdin.
                    # Neither normal nor exceptional cleanup waits out their runtime.
                    writer.join(timeout=1)
                    if writer.is_alive():
                        prompt_cancelled = True
                        try:
                            process.stdin.close()
                        except (OSError, ValueError) as exc:
                            prompt_errors.append(exc)
                        writer.join(timeout=0.1)
                if can_handle:
                    signal.signal(signal.SIGTERM, previous)
                if follower is not None:
                    stop_forwarding.set()
                    follower.join()
                    forward_stdout()
        if not prompt_cancelled:
            for exc in prompt_errors:
                if not isinstance(exc, BrokenPipeError):
                    raise exc
        if capture_stdout:
            size = output.seek(0, 2)
            output.seek(max(0, size - 16000))
            tail = output.read().decode("utf-8", errors="replace")[-4000:]
        else:
            tail = ""
    return process.returncode, timed_out, tail
