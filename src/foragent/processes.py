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
from foragent.shelf_io import open_file


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

# Cumulative paused seconds of the current run (an open talk terminal, D55 addendum).
# The runner sets it for a whole run, so every command's timeout (executor
# sessions, Done when and unit checks, baseline capture, local CI) grows by the
# time paused while that command ran. Without it a timeout stays fixed.
_paused_seconds: ContextVar[Callable[[], float] | None] = ContextVar("paused_seconds", default=None)
# A ContextVar does not reach threads started without a copied context (thread pools,
# plain threads), so the run's pause is also kept process-wide: a runner process
# runs one issue, and its worker threads' commands pause with it.
_process_paused: Callable[[], float] | None = None


@contextmanager
def extend_deadline(paused: Callable[[], float]) -> Iterator[None]:
    """Within this context, ``run_logged`` adds the growth of ``paused()`` to each timeout, in every thread."""
    global _process_paused
    token = _paused_seconds.set(paused)
    previous, _process_paused = _process_paused, paused
    try:
        yield
    finally:
        _process_paused = previous
        _paused_seconds.reset(token)


def current_pause() -> Callable[[], float] | None:
    """The pause clock of the run this thread works for, if any."""
    return _paused_seconds.get() or _process_paused


def _paused_now(paused: Callable[[], float]) -> float | None:
    try:
        return float(paused())
    except Exception:  # noqa: BLE001 - an unreadable record keeps the original limit
        return None


def _wait_with_deadline(process, timeout_s: float) -> None:
    """Wait like ``process.wait(timeout_s)``, extended by time paused since the wait began.

    Inside a review stop scope the wait also ends with ReviewStopped once the review stops,
    even when the stop had to leave this process running (fail closed).
    """
    scope = _stop_scope.get()
    if scope is not None:
        end = time.monotonic() + timeout_s
        while True:
            scope.refuse_launch()
            left = end - time.monotonic()
            try:
                process.wait(timeout=max(0.0, min(0.1, left)))
                return
            except subprocess.TimeoutExpired:
                if left <= 0:
                    raise
    paused = current_pause()
    base = _paused_now(paused) if paused is not None else None
    granted = 0.0
    timeout = timeout_s
    while True:
        try:
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            current = _paused_now(paused) if base is not None else None
            if current is None:
                raise
            extra = current - base - granted
            if extra <= 0:
                raise
            granted += extra
            timeout = extra


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
        # The review stop's verifier: a target whose ancestor row is missing or unreadable
        # gets no signal (fail closed). Other callers keep their lineage walk.
        rows, unreadable = _scan()
        lineage = _protected(rows)[0]
        recorded = {item["pid"]: item["start_time"] for item in identities}
        return {pid: rows[pid] for pid, start in recorded.items()
                if pid > 1 and unverified_reason(pid, start, rows, unreadable, recorded, lineage) is None}

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


# Only presentation settings may cross from the person's environment into a try.
_TRY_PRESENTATION = ("LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
                     "TERM", "COLORTERM", "COLUMNS", "LINES")
# Per-user services a throwaway HOME cannot separate: launchd's gui/<uid> jobs (the
# person's live gate) and AppleScript (notifications and terminal windows). A try finds
# these names first on PATH and they refuse, so they never reach the person's own.
_SHARED_SERVICES = ("launchctl", "osascript")
_TRY_BIN = ".foragent-try/bin"


def try_home(home: Path, language: str) -> Path:
    """Make a throwaway home: only the person's language as personal config, shared services refused."""
    config = home / ".config/foragent/config.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"language": language}, ensure_ascii=False) + "\n", encoding="utf-8")
    shims = home / _TRY_BIN
    shims.mkdir(parents=True, exist_ok=True)
    for name in _SHARED_SERVICES:
        shim = shims / name
        shim.write_text(
            f"#!/bin/sh\necho 'foragent try: {name} is not available; it would reach your own "
            f"machine services' >&2\nexit 1\n", encoding="utf-8")
        shim.chmod(0o755)
    return home


def try_environment(home: Path, cwd: Path) -> dict[str, str]:
    """Build the complete try environment, used with run_logged(inherit_env=False).

    Start empty so even unknown config roots, tool destinations and session handles
    cannot escape the throwaway home. Only tool lookup, presentation and the uv
    content cache come from the parent. Prepare home with try_home first.
    """
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = _child_environment(cwd).get("PATH", os.defpath)
    env = {key: os.environ[key] for key in _TRY_PRESENTATION if key in os.environ}
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "PATH": os.pathsep.join(entry for entry in (str(home / _TRY_BIN), path) if entry),
        "FORAGENT_VIEW": "off",
        "FORAGENT_UPDATE_CHECK": "off",
        "FORAGENT_ORCH_SPAWN": "off",
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR") or str(cache_home / "uv"),
    })
    return env


def run_logged(
    argv: list[str], *, cwd: Path, timeout_s: int, log_path: Path,
    prompt: str | None = None, capture_stdout: bool = False,
    stdout_path: Path | None = None, terminate_children: bool = False,
    stderr_path: Path | None = None, env: dict[str, str | None] | None = None,
    inherit_env: bool = True,
) -> tuple[int | None, bool, str]:
    """Append output without unbounded buffering; env overlays (None removes a key).

    With inherit_env=False, env is the complete environment, with no parent values.
    """
    child_env = {**(_child_environment(cwd) if inherit_env else {}), **(env or {})}
    child_env = {key: value for key, value in child_env.items() if value is not None}
    on_process_group = _process_group_callback.get()
    on_process_tree = _process_tree_callback.get()
    scope = _stop_scope.get()
    if scope is not None:
        scope.refuse_launch()  # A stopped review starts no new process.
        observed_tree = on_process_tree

        def on_process_tree(identities, observed_tree=observed_tree):
            scope.record(identities)
            if observed_tree is not None:
                observed_tree(identities)
    with open_file(log_path, "ab") as log, (
        open_file(stdout_path, "w+b") if stdout_path is not None else tempfile.TemporaryFile()
    ) as output, (open_file(stderr_path, "ab") if stderr_path is not None else nullcontext()) as errors:
        process = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
            stdout=output if capture_stdout else log,
            stderr=errors if errors is not None else log if capture_stdout else subprocess.STDOUT,
            # Closing stdin during cancellation must not wait for a buffered
            # writer's lock while a descendant keeps the pipe open.
            bufsize=0,
            start_new_session=True, env=child_env,
        )
        if scope is not None:
            # Record the launch before the slower first tree scan, so a stop meanwhile reaches it.
            try:
                root = read_process_identity(process.pid)
            except (OSError, psutil.Error, SystemError):
                root = None
            if root is not None:
                scope.record([{"pid": root.pid, "start_time": root.start_time}])
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
        # Inside a review stop scope, the review's own handler stops every recorded tree.
        can_handle = threading.current_thread() is threading.main_thread() and scope is None

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
                _wait_with_deadline(process, timeout_s)
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
                try:
                    identities = observe_tree(identities)
                except (OSError, psutil.Error):
                    if scope is None or scope.signum is None:
                        raise
                    # The stop already names a failed scan; keep the stop as the outcome.
                if scope is not None and scope.signum is not None:
                    # The review stop owns the stop: it signals what it verifies and names the
                    # rest, so cleanup neither signals again nor waits on a process left running.
                    scope.record(identities)
                elif process.poll() is None:
                    stop_children()
                    process.wait()
                if (terminate_children or observation_errors) and (scope is None or scope.signum is None):
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


class ReviewStopped(BaseException):
    """A signal stopped a review; ``left`` names processes left running and why.

    A BaseException, like KeyboardInterrupt: no ``except Exception`` turns it into a verdict.
    """

    def __init__(self, signum: int, left: tuple[tuple[int | None, str], ...] = ()):
        super().__init__(signum)
        self.signum, self.left = signum, tuple(left)


class StopScope:
    """Every process tree that ``run_logged`` starts inside a review, for one prompt stop.

    Records outlive ``run_logged``: helpers a reviewer leaves behind are still stopped. A
    record that arrives after the stop (a launch still being observed, a child seen later)
    is stopped on arrival. ``left`` collects every live process left unsignalled, with why.

    A signal handler calls ``interrupt`` on the main thread, possibly while that thread is
    inside ``record`` or inside the stop itself, so neither takes a lock the handler could
    wait on: records and reasons are appended to lists (atomic), and the stop's state uses
    a reentrant lock held only for flag updates.
    """

    def __init__(self) -> None:
        self._state = threading.RLock()
        self._records: list[tuple[int, float]] = []
        self._handled: set[int] = set()
        self._owner: int | None = None
        self._done = threading.Event()
        self.signum: int | None = None
        self.left: list[tuple[int | None, str]] = []

    def record(self, identities) -> None:
        items = [(item["pid"], item["start_time"]) for item in identities]
        self._records.extend(items)
        if self._done.is_set():
            self._stop_late()

    def _recorded(self) -> dict[int, float]:
        recorded: dict[int, float] = {}
        for pid, start in list(self._records):
            recorded.setdefault(pid, start)
        return recorded

    def _stop_late(self) -> None:
        recorded = self._recorded()
        with self._state:
            fresh = {pid: start for pid, start in recorded.items() if pid not in self._handled}
            self._handled.update(fresh)
        if fresh:
            self.left.extend(_stop_verified(fresh, known=recorded))

    def stopped(self) -> ReviewStopped | None:
        return None if self.signum is None else ReviewStopped(self.signum, tuple(self.left))

    def refuse_launch(self) -> None:
        if (error := self.stopped()) is not None:
            raise error

    def interrupt(self, signum: int) -> ReviewStopped | None:
        """Stop once and return the exception to raise.

        A second signal on the thread that is running the stop returns None (ignored), so the
        stop finishes; another thread waits for the stop and gets the same result.
        """
        me = threading.get_ident()
        with self._state:
            first = self._owner is None
            if first:
                self._owner, self.signum = me, signum
            elif self._owner == me and not self._done.is_set():
                return None
        if first:
            try:
                recorded = self._recorded()
                with self._state:
                    self._handled.update(recorded)
                self.left.extend(_stop_verified(recorded))
            finally:
                self._done.set()
            self._stop_late()  # Records that arrived while the first stop ran.
        else:
            self._done.wait()
        return self.stopped()


_stop_scope: ContextVar[StopScope | None] = ContextVar("stop_scope", default=None)


@contextmanager
def stop_scope() -> Iterator[StopScope]:
    scope = StopScope()
    token = _stop_scope.set(scope)
    try:
        yield scope
    finally:
        _stop_scope.reset(token)


def refuse_stopped() -> None:
    """Raise ReviewStopped when this context's review has stopped; otherwise do nothing."""
    scope = _stop_scope.get()
    if scope is not None:
        scope.refuse_launch()


def pause(seconds: float, sleep: Callable[[float], None]) -> None:
    """``sleep(seconds)``, ended by a review stop with ReviewStopped.

    Outside a review stop scope this is exactly one ``sleep(seconds)`` call.
    """
    scope = _stop_scope.get()
    if scope is None:
        sleep(seconds)
        return
    end = time.monotonic() + seconds
    while True:
        scope.refuse_launch()
        left = end - time.monotonic()
        if left <= 0:
            return
        sleep(min(0.1, left))


def _scan() -> tuple[dict[int, ProcessIdentity], set[int]]:
    table = list_processes()
    return {row.pid: row for row in table}, set(getattr(table, "unreadable", ()))


def _stable_scan() -> tuple[dict[int, ProcessIdentity], set[int], dict[int, ProcessIdentity]]:
    """Two listings: the latest rows, every unreadable PID, and the rows stable across both.

    Enumeration is not atomic: one listing can hold a parent's row read before its PID was
    reused and a child of the replacement read after it. A row with the same start time and
    parent in both listings existed before the second began, so a parent stable in both really
    was that child's parent. Only stable rows may add a child to a stop (``_descendants``).
    """
    (earlier, unread_first), (rows, unread_second) = _scan(), _scan()
    stable = {pid: row for pid, row in rows.items() if pid in earlier
              and (row.start_time, row.ppid) == (earlier[pid].start_time, earlier[pid].ppid)}
    return rows, unread_first | unread_second, stable


def _own_lineage(rows, unreadable) -> set[int]:
    """fa's own ancestors up to pid 1; an unreadable or missing row means it cannot be known."""
    lineage, current = {1, os.getpid()}, os.getpid()
    while current != 1:
        row = rows.get(current)
        if row is None or current in unreadable:
            raise PermissionError(f"fa's own ancestor {current} is unreadable; ownership cannot be verified")
        current = row.ppid
        if current in lineage and current != 1:
            raise PermissionError(f"fa's own ancestry has a cycle at {current}; ownership cannot be verified")
        lineage.add(current)
    return lineage


def unverified_reason(pid: int, start_time: float, rows, unreadable, recorded, lineage) -> str | None:
    """Why ``pid`` may not be signalled, or None when it is a verified recorded member.

    Verified: a readable row with the recorded start time, outside fa's lineage, group and
    session, whose parent chain of readable rows reaches pid 1, fa itself or a verified
    recorded member. Each missing or unreadable link refuses (fail closed).
    """
    group, session = os.getpgrp(), os.getsid(0)
    seen: dict[int, str | None] = {}

    def check(target: int, start: float) -> str | None:
        if target in seen:
            return seen[target]
        seen[target] = "its ancestry has a cycle"
        if target in unreadable:
            reason = "its process row is unreadable"
        elif target not in rows:
            reason = GONE
        elif rows[target].start_time != start:
            reason = REUSED
        elif target in lineage or rows[target].pgid in lineage or rows[target].sid in lineage \
                or rows[target].pgid == group or rows[target].sid == session:
            reason = "it belongs to fa's own process lineage, group or session"
        else:
            reason, parent, walked = None, rows[target].ppid, set()
            while parent not in (1, os.getpid()):
                if parent in walked:
                    reason = f"ancestor {parent} forms a cycle"
                    break
                walked.add(parent)
                if parent in unreadable:
                    reason = f"ancestor {parent} is unreadable"
                    break
                if parent not in rows:
                    reason = f"ancestor {parent} is missing from the process table"
                    break
                if parent in recorded and rows[parent].start_time == recorded[parent]:
                    if check(parent, recorded[parent]) is not None:
                        reason = f"ancestor {parent} could not be verified"
                    break
                parent = rows[parent].ppid
        seen[target] = reason
        return reason

    return check(pid, start_time)


GONE = "it is gone"
REUSED = "its start time differs from the recorded one (reused pid)"


def _descendants(recorded: dict[int, float], rows, unreadable, lineage, stable) -> dict[int, float]:
    """``recorded`` plus live children of verified members, born after their parent.

    A child and its parent must both be ``stable`` (``_stable_scan``), with the parent's start
    time equal to the member's, so a PID reused during enumeration never authorizes a child.
    """
    members = dict(recorded)
    while True:
        verified = {pid for pid, start in members.items()
                    if unverified_reason(pid, start, rows, unreadable, members, lineage) is None}
        children = {row.pid: row.start_time for row in stable.values()
                    if row.pid not in members and row.ppid in verified and row.ppid in stable
                    and stable[row.ppid].start_time == members[row.ppid]
                    and row.start_time >= members[row.ppid]}
        if not children:
            return members
        members.update(children)


def _signal_fresh(pid: int, start: float, signum: int) -> str | None:
    """Send ``signum`` only if a read just before it still shows ``pid`` born at ``start``.

    Returns None when sent or gone, else why it was not sent. On Linux the pidfd, opened
    before the read, binds the signal to the process that was read.
    """
    descriptor = None
    try:
        if callable(getattr(os, "pidfd_open", None)) and callable(getattr(signal, "pidfd_send_signal", None)):
            try:
                descriptor = os.pidfd_open(pid, 0)
            except ProcessLookupError:
                return None
            except OSError as exc:
                if exc.errno != errno.ENOSYS:
                    raise
        try:
            current = read_process_identity(pid)
        except SystemError as exc:
            if not _caused_by_permission_error(exc):
                raise
            return "its identity could not be read just before the signal"
        except (OSError, psutil.Error):
            return "its identity could not be read just before the signal"
        if current is None:
            return None
        if current.start_time != start:
            return REUSED
        if descriptor is not None:
            signal.pidfd_send_signal(descriptor, signum)
        else:
            os.kill(pid, signum)
        return None
    except ProcessLookupError:
        return None
    except OSError as exc:
        return f"signal failed: {exc}"
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _stop_verified(recorded: dict[int, float], *, known: dict[int, float] | None = None, wait_s: float = 3.0):
    """SIGTERM each verified member of ``recorded`` and its newer children, then SIGKILL survivors.

    ``known`` is every recorded identity (ancestors of a late record). Children go first and
    no group signal is sent. Returns ``(pid or None, reason)`` for live processes left running.
    """
    if not recorded:
        return []
    known = {**(known or {}), **recorded}
    signalled: dict[int, float] = {}
    left: dict[int | None, str] = {}

    def stage(signum: int, targets: dict[int, float] | None) -> bool:
        try:
            rows, unreadable, stable = _stable_scan()
            lineage = _own_lineage(rows, unreadable)
        except (OSError, psutil.Error) as exc:
            # A failed scan authorizes no signal at all.
            left[None] = str(exc)
            return False
        members = _descendants(known, rows, unreadable, lineage, stable)
        if targets is None:
            targets = {pid: start for pid, start in members.items()
                       if pid in recorded or pid not in known}
        for pid, start in sorted(targets.items(), key=lambda item: -item[1]):  # children are born later
            reason = unverified_reason(pid, start, rows, unreadable, members, lineage)
            if reason is None:
                reason = _signal_fresh(pid, start, signum)
                if reason is None:
                    signalled[pid] = start
            if reason is None or reason == GONE:
                left.pop(pid, None)
            else:
                left[pid] = reason
        return True

    if not stage(signal.SIGTERM, None):
        return list(left.items())
    deadline = time.monotonic() + wait_s
    for _ in range(int(wait_s / 0.1)):
        try:
            rows, _unreadable = _scan()
        except (OSError, psutil.Error):
            break
        if not any(pid in rows and rows[pid].start_time == start for pid, start in signalled.items()):
            return list(left.items())
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    stage(signal.SIGKILL, dict(signalled))
    return list(left.items())
