"""`fa lanes talk`: watch or talk to an issue's worker (D55 addendum).

A running issue opens its live log (``tail -F``) in a terminal. A message goes
to a running Codex worker through ``codex queue``; for Claude it is saved as the
next attempt's hint. A finished or blocked issue opens ``codex resume`` /
``claude --resume`` instead. While a recorded talk terminal of a running issue
stays open the run is ``talking``: the gate tick pauses its stall and runtime
checks, and the first tick that finds the terminal closed ends the talk.

The terminal record lives in ``<state>/talk/<n>.json``. Terminals opened through
Terminal.app have no handle, so they are not recorded and pause nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

from foragent import design_session, orch_spawn
from foragent.config import Config
from foragent.i18n import text
from foragent.lanes import LaneError
from foragent.run_previous import hint_path
from foragent.state import StateStore
from foragent.state_records import Run

QUEUE_TIMEOUT = 30
OPEN_TIMEOUT = 10


class TalkError(LaneError):
    """The talk command could not reach the issue's session."""


def talk_path(store: StateStore, number: int) -> Path:
    return store.root / "talk" / f"{number}.json"


def _lane_executor(run: Run, config: Config) -> str:
    settings = config.lanes.get(run.lane, {}) if run.lane != "main" else {}
    return settings.get("executor") or config.executor


def _session(run: Run, config: Config) -> tuple[str, str | None]:
    """The executor to talk to and its session, or None when no usable session is recorded.

    A running issue talks to its lane's implementer; a session recorded for
    another executor (an earlier run's) is not that worker's. A finished run
    resumes the session it recorded.
    """
    if run.status != "running":
        executor = run.session_executor or _lane_executor(run, config)
        return executor, run.session_id
    executor = _lane_executor(run, config)
    usable = run.session_id if run.session_executor in (None, executor) else None
    return executor, usable


@contextmanager
def _issue_lock(store: StateStore, number: int, *, blocking: bool = True) -> Iterator[bool]:
    """Serialize one issue's talk lifecycle (open, send, record, end); yields False if busy."""
    path = store.root / "talk" / f"{number}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _call(argv: list[str], cwd: Path, timeout: int = OPEN_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          check=True, timeout=timeout)


def _alive(record: dict, cwd: Path, *, show: bool) -> bool:
    """Whether a recorded terminal is still open; ``show`` also brings it forward."""
    executable, handle = record.get("executable"), record.get("handle")
    if not isinstance(executable, str) or not isinstance(handle, str):
        return False
    try:
        if record.get("kind") == "orca":
            _call([executable, "terminal", "show", "--terminal", handle, "--json"], cwd)
            if show:
                _call([executable, "terminal", "switch", "--terminal", handle], cwd)
        elif show:
            _call([executable, "select-window", "-t", handle], cwd)
        else:
            windows = _call([executable, "list-windows", "-a", "-F", "#{window_id}"], cwd).stdout.split()
            return handle in windows
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _read_record(store: StateStore, number: int) -> dict | None:
    try:
        record = json.loads(talk_path(store, number).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _open(store: StateStore, root: Path, cwd: Path, title: str, command: list[str]) -> tuple[str, dict | None]:
    """Open ``command`` in ``cwd``: Orca, tmux, then Terminal.app; returns (screen, record)."""
    line = shlex.join(command)
    if orca := orch_spawn._orca(store, root):
        # Orca starts a terminal in the selected worktree; it knows issue worktrees by path.
        result = _call([orca, "terminal", "create", "--worktree", f"path:{cwd}", "--title", title,
                        "--command", line, "--json"], cwd)
        try:
            handle = design_session._handle(json.loads(result.stdout))
        except ValueError:
            handle = None
        return "orca", {"kind": "orca", "executable": orca, "handle": handle} if handle else None
    tmux = shutil.which("tmux") if os.environ.get("TMUX") else None
    if tmux:
        result = _call([tmux, "new-window", "-P", "-F", "#{window_id}", "-n", title, "-c", str(cwd), line], cwd)
        handle = result.stdout.strip()
        return "tmux", {"kind": "tmux", "executable": tmux, "handle": handle} if handle else None
    osascript = shutil.which("osascript")
    if osascript:
        # The shell line goes in as an argument, so no AppleScript quoting is needed.
        _call([osascript, "-e", "on run argv", "-e", 'tell application "Terminal" to activate',
               "-e", 'tell application "Terminal" to do script (item 1 of argv)', "-e", "end run",
               f"cd {shlex.quote(str(cwd))} && {line}"], cwd)
        return "Terminal", None
    return "", None


def _save_hint(store: StateStore, number: int, message: str) -> None:
    path = hint_path(store.root, number)
    with store._state_lock():
        unused = path.read_text(encoding="utf-8") if path.is_file() else ""
        store._atomic_write(path, f"{unused.rstrip()}\n\n{message}" if unused.strip() else message)


def _queue(thread: str, message: str, cwd: Path, number: int, language: str) -> None:
    codex = shutil.which("codex") or "codex"
    try:
        _call([codex, "queue", "--thread", thread, "--message", message], cwd, timeout=QUEUE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise TalkError(text("talk_queue_failed", language, n=number,
                             error=" ".join(str(detail).split())[:200])) from exc


def talk(number: int, *, message: str | None = None, store: StateStore, config: Config,
         repo_root: Path | None = None, now: datetime | None = None) -> str:
    """Open the issue's live log or resume terminal; return the one line to print."""
    language = config.language
    root = (repo_root or Path.cwd()).resolve()
    run = store.get_run(number)
    if run is None:
        raise TalkError(text("talk_no_session", language, n=number))
    executor, session = _session(run, config)
    if session is None and not (run.status == "running" and executor == "claude"):
        raise TalkError(text("talk_no_session", language, n=number))
    with _issue_lock(store, number):
        # Another talk may have changed the run while this one waited for the lock.
        run = store.get_run(number)
        executor, session = _session(run, config)
        running = run.status == "running"
        if session is None and not (running and executor == "claude"):
            raise TalkError(text("talk_no_session", language, n=number))
        return _talk_locked(number, run, executor, session, message, store, root, now, language)


def _talk_locked(number: int, run: Run, executor: str, session: str | None, message: str | None,
                 store: StateStore, root: Path, now: datetime | None, language: str) -> str:
    running = run.status == "running"
    worktree = store.worktree_path(number)
    cwd = worktree if worktree.is_dir() else root
    if running:
        title = f"talk #{number}"
        # -F follows the log across recreation; -f is kept for tails that lack -F.
        command = ["tail", "-f", "-F", str(store.log_path(number))]
    else:
        title = f"resume #{number}"
        program = shutil.which(executor) or executor
        extra = [message] if message else []
        command = ([program, "resume", session, *extra, "-C", str(cwd)] if executor == "codex"
                   else [program, "--resume", session, *extra])
    began = False
    if running and not run.talking:
        # Talking starts before the terminal opens, so no tick stops the worker meanwhile;
        # every way this command ends without a recorded terminal ends the talk again.
        began = store.begin_talk(number, run.started_at, _now(store, now)) is not None
    talking = run.talking or began
    try:
        record = _read_record(store, number)
        reshown = talking and record is not None and _alive(record, cwd, show=True)
        if reshown:
            screen = record["kind"]
        else:
            with orch_spawn.watch_open._opening(store):  # Shared with the gate's other openers.
                screen, record = _open(store, root, cwd, title, command)
        sent = ""
        if running and message:
            if executor == "codex":
                _queue(session, message, cwd, number, language)
                sent = text("talk_sent", language, n=number)
            else:
                _save_hint(store, number, message)
                sent = text("talk_hint_saved", language, n=number)
        published = record is not None and store.publish_talk(
            number, run.started_at, run.status, {**record, "number": number, "running": running})
    except BaseException:
        if talking:  # A failed open or send leaves the terminal unrecorded and nothing paused.
            store.end_talk(number, _now(store, now), restart_stall=False)
        raise
    if talking and not (running and published):
        store.end_talk(number, _now(store, now), restart_stall=False)
    if not screen:
        return " ".join(filter(None, [sent, text("talk_manual", language, command=f"cd {shlex.quote(str(cwd))} && "
                                                  + shlex.join(command))]))
    opened = text("talk_reshown" if reshown else ("talk_log_opened" if running else "talk_resume_opened"),
                  language, n=number, screen=screen)
    parts = [sent or opened]
    if running and record is None:
        parts.append(text("talk_no_handle", language))
    return " ".join(parts)


def _now(store: StateStore, now: datetime | None) -> datetime:
    return now or datetime.fromisoformat(store._timestamp())


def check_talking(store: StateStore, run: Run, root: Path, now: datetime) -> tuple[Run, bool]:
    """Tick step for a talking run: ``(run, still_open)``; a closed terminal ends the talk.

    Ending adds the paused time to ``waited_seconds`` and restarts the stall
    clock (``last_progress_at``) at ``now``. A talk command in progress for the
    issue holds its lock; the tick then treats the talk as open and looks again
    next tick, so it never ends a talk that is being opened.
    """
    with _issue_lock(store, run.number, blocking=False) as held:
        if not held:
            return run, True
        current = store.get_run(run.number)
        if current is None or current.status != "running" or not current.talking:
            return current or run, False
        record = _read_record(store, run.number)
        worktree = Path(current.worktree)
        if record is not None and _alive(record, worktree if worktree.is_dir() else root, show=False):
            return current, True
        return end_talk(store, current, now), False


def end_talk(store: StateStore, run: Run, now: datetime) -> Run:
    """Credit the paused time and drop the record in one state-lock step."""
    return store.end_talk(run.number, now) or run
