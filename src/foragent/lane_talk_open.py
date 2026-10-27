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
from uuid import uuid4

from foragent import orch_spawn
from foragent.config import Config
from foragent.i18n import text
from foragent.lane_talk import (
    RecordUnreadable,
    _alive,
    _call,
    _orca_handle,
    _read_record,
    find_pending,
    talk_path,  # noqa: F401 - re-exported for callers of the command side
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


def _opener(store: StateStore, root: Path) -> dict | None:
    """The terminal opener, chosen before anything opens: Orca, tmux, then Terminal.app."""
    if orca := orch_spawn._orca(store, root):
        return {"kind": "orca", "executable": orca}
    tmux = shutil.which("tmux") if os.environ.get("TMUX") else None
    if tmux:
        # TMUX is "<server socket>,<pid>,<session>"; every later call targets that server.
        return {"kind": "tmux", "executable": tmux, "socket": os.environ["TMUX"].split(",", 1)[0]}
    osascript = shutil.which("osascript")
    return {"kind": "Terminal", "executable": osascript} if osascript else None


def _launch(opener: dict, cwd: Path, title: str, command: list[str]) -> str | None:
    """Open ``command`` in ``cwd`` with ``opener``; return the terminal's handle, if it has one."""
    line, executable = shlex.join(command), opener["executable"]
    if opener["kind"] == "orca":
        # Orca starts a terminal in the selected worktree; it knows issue worktrees by path.
        return _orca_handle(_call([executable, "terminal", "create", "--worktree", f"path:{cwd}", "--title", title,
                                   "--command", line, "--json"], cwd).stdout)
    if opener["kind"] == "tmux":
        handle = _call([executable, "-S", opener["socket"], "new-window", "-P", "-F", "#{window_id}", "-n", title,
                        "-c", str(cwd), line], cwd).stdout.strip()
        return handle or None
    # The shell line goes in as an argument, so no AppleScript quoting is needed.
    _call([executable, "-e", "on run argv", "-e", 'tell application "Terminal" to activate',
           "-e", 'tell application "Terminal" to do script (item 1 of argv)', "-e", "end run",
           f"cd {shlex.quote(str(cwd))} && {line}"], cwd)
    return None


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
    # A unique title lets a later tick find a terminal whose handle was never saved.
    token = uuid4().hex[:8]
    if running:
        title = f"talk #{number} {token}"
        # -F follows the log across recreation; -f is kept for tails that lack -F.
        command = ["tail", "-f", "-F", str(store.log_path(number))]
    else:
        title = f"resume #{number} {token}"
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
    opener = _opener(store, root)
    trackable = opener is not None and opener["kind"] in {"orca", "tmux"}
    pending = ({**opener, "title": title, "number": number, "running": running}
               if opener is not None else None)
    state = None
    if running and run.talking and record is not None:
        if isinstance(record.get("handle"), str):
            state = _alive(record, cwd, show=True)
        else:
            state, handle = find_pending(record, cwd)
            if state:
                record = {**record, "handle": handle}
                store.publish_talk(number, run.started_at, "running", record)
                state = _alive(record, cwd, show=True)
    # Only a confirmed closure (a stale handle, or tmux on its socket without the window) opens
    # another terminal; an open, pending or unknown one is never doubled.
    reshown = state is not None and state is not False or (state is None and run.talking and record is not None)
    began = False
    if running and not run.talking and trackable:
        # Talking and the pending record start together, before anything opens, so no tick stops
        # the worker meanwhile and a later tick can find the terminal even if this command dies.
        began = store.begin_talk(number, run.started_at, _now(store, now), pending) is not None
    elif running and run.talking and not reshown and trackable:
        store.publish_talk(number, run.started_at, "running", pending)  # Replaces a closed terminal's record.
    talking = run.talking or began
    handle = None
    screen = ""
    try:
        if reshown:
            screen = record["kind"]
        elif opener is not None:
            screen = opener["kind"]
            with orch_spawn.watch_open._opening(store):  # Shared with the gate's other openers.
                handle = _launch(opener, cwd, title, command)
    except subprocess.SubprocessError as exc:
        # A timeout, a crash or a failed reply (even a signal, or an error after creating the
        # terminal) does not prove nothing opened: the pending record keeps the talk.
        raise TalkError(text("talk_open_unknown", language, n=number)) from exc
    except OSError:
        # The opener could not start at all, so no terminal exists and nothing is left to pause for.
        if talking and not reshown:
            store.end_talk(number, _now(store, now), restart_stall=False)
        raise
    # Any other interruption may come after the terminal opened: the pending record stays.
    try:
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
    except BaseException:
        if talking:  # A failed send leaves the terminal unrecorded and nothing paused (the decision).
            store.end_talk(number, _now(store, now), restart_stall=False)
        raise
    if not reshown and handle is not None:
        store.publish_talk(number, run.started_at, run.status, {**pending, "handle": handle})
    # A trackable opener without a handle keeps its pending record: a tick finds it by title.
    if not screen:
        return " ".join(filter(None, [sent, text("talk_manual", language, command=f"cd {shlex.quote(str(cwd))} && "
                                                  + shlex.join(command))]))
    if reshown and state is None:
        opened = text("talk_probe_unknown", language, n=number)
    else:
        opened = text("talk_reshown" if reshown else ("talk_log_opened" if running else "talk_resume_opened"),
                      language, n=number, screen=screen)
    parts = [sent or opened]
    if running and not trackable:
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

