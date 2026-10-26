"""`fa lanes talk`: open an issue's live log or session and send a message (D55 addendum).

A running issue opens its live log (``tail -F``) in a terminal. A message goes
to a running Codex worker through ``codex queue``; for Claude it is saved as the
next attempt's hint. A finished or blocked issue opens ``codex resume`` /
``claude --resume`` instead. Terminals opened through Terminal.app have no
handle, so they are not recorded and pause nothing. Records, probes and talk
endings live in ``lane_talk``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from foragent import orch_spawn
from foragent.config import Config
from foragent.i18n import text
from foragent.lane_talk import (  # noqa: F401
    RecordUnreadable,
    _alive,
    _call,
    _orca_handle,
    _read_record,
    talk_path,
)
from foragent.lanes import LaneError
from foragent.run_previous import hint_path
from foragent.state import StateStore
from foragent.state_records import Run

QUEUE_TIMEOUT = 30


class TalkError(LaneError):
    """The talk command could not reach the issue's session."""


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
    # A stale session (a fresh thread is starting, or inherited and not resumed) is not the worker's.
    usable = run.session_id if run.session_executor in (None, executor) and not run.session_stale else None
    return executor, usable


def _open(store: StateStore, root: Path, cwd: Path, title: str, command: list[str]) -> tuple[str, dict | None]:
    """Open ``command`` in ``cwd``: Orca, tmux, then Terminal.app; returns (screen, record)."""
    line = shlex.join(command)
    if orca := orch_spawn._orca(store, root):
        # Orca starts a terminal in the selected worktree; it knows issue worktrees by path.
        result = _call([orca, "terminal", "create", "--worktree", f"path:{cwd}", "--title", title,
                        "--command", line, "--json"], cwd)
        handle = _orca_handle(result.stdout)
        return "orca", {"kind": "orca", "executable": orca, "handle": handle} if handle else None
    tmux = shutil.which("tmux") if os.environ.get("TMUX") else None
    if tmux:
        # TMUX is "<server socket>,<pid>,<session>"; every later call targets that server.
        socket = os.environ["TMUX"].split(",", 1)[0]
        result = _call([tmux, "-S", socket, "new-window", "-P", "-F", "#{window_id}", "-n", title,
                        "-c", str(cwd), line], cwd)
        handle = result.stdout.strip()
        return "tmux", ({"kind": "tmux", "executable": tmux, "socket": socket, "handle": handle}
                        if handle else None)
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
    with store.talk_lock(number):
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
    try:
        record = _read_record(store, number)
    except RecordUnreadable as exc:
        if run.talking:  # An open talk's terminal may be open: never open a second one or end that talk.
            raise TalkError(text("talk_record_unreadable", language, n=number, path=str(exc))) from exc
        record = None  # No talk is open, so the record is stale.
    began = False
    if running and not run.talking:
        # Talking starts before the terminal opens, so no tick stops the worker meanwhile;
        # every way this command ends without a recorded terminal ends the talk again.
        began = store.begin_talk(number, run.started_at, _now(store, now)) is not None
    talking = run.talking or began
    try:
        # An unanswered probe is not a closed terminal: never open a second one beside it.
        reshown = talking and record is not None and _alive(record, cwd, show=True) is not False
        if reshown:
            screen = record["kind"]
        else:
            with orch_spawn.watch_open._opening(store):  # Shared with the gate's other openers.
                screen, record = _open(store, root, cwd, title, command)
        sent = ""
        if running and message:
            if executor == "codex":
                # The session may have changed while the terminal opened; the runner records a new
                # one under this same lock, so the one read now is the worker's until the send ends.
                session = _current_session(store, run, number, language)
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


def _current_session(store: StateStore, run: Run, number: int, language: str) -> str:
    """The running worker's session now; the run must be the same run that the terminal opened for."""
    current = store.get_run(number)
    if current is None or current.started_at != run.started_at or current.status != "running":
        raise TalkError(text("talk_run_changed", language, n=number))
    if current.session_stale:  # The worker switched to a thread whose ID is not recorded yet.
        raise TalkError(text("talk_session_starting", language, n=number))
    session = current.session_id if current.session_executor in (None, "codex") else None
    if not session:
        raise TalkError(text("talk_no_session", language, n=number))
    return session


def _now(store: StateStore, now: datetime | None) -> datetime:
    return now or datetime.fromisoformat(store._timestamp())

