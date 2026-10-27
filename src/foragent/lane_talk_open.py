"""`fa lanes talk`: open an issue's live log or session and send a message (D55 addendum, D95).

A running issue opens its live log (``tail -F``) in a terminal. A message goes to a
running Codex worker through ``codex queue``; for Claude it is saved as the next
attempt's hint. A finished or blocked issue opens ``codex resume`` / ``claude
--resume`` instead. The command never writes the run record: after the terminal
opens it appends one ``talk-opened`` event, and the gate tick owns the talk from
there (``lane_talk``). Every judgement of a terminal's reply is in ``lane_talk_probe``.
Terminal.app gives no handle, so its terminals get no event and pause nothing.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from uuid import uuid4

from foragent import lane_talk_probe as probe, orch_spawn
from foragent.config import Config
from foragent.i18n import text
from foragent.lanes import LaneError
from foragent.orch_events import append_event
from foragent.run_previous import hint_path
from foragent.state import StateStore
from foragent.state_locks import LockBusy
from foragent.state_records import Run
from foragent.state_talk import TALK_OPENED

QUEUE_TIMEOUT = 30


def _call(argv: list[str], cwd: Path, timeout: int = probe.OPEN_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          check=True, timeout=timeout)


class TalkError(LaneError):
    """The talk command failed; ``stdout`` is a line it still owes (a message it delivered)."""

    def __init__(self, message: str, stdout: str = ""):
        super().__init__(message)
        self.stdout = stdout


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
        return probe.created_handle(_call([executable, "terminal", "create", "--worktree", f"path:{cwd}",
                                           "--title", title, "--command", line, "--json"], cwd).stdout)
    if opener["kind"] == "tmux":
        return probe.created_window(_call([executable, "-S", opener["socket"], "new-window", "-P", "-F",
                                           "#{window_id}", "-n", title, "-c", str(cwd), line], cwd).stdout)
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


TALK_LOCK_WAIT = 10  # Seconds `fa lanes talk` waits for the issue's worktree lock.


def talk(number: int, *, message: str | None = None, store: StateStore, config: Config,
         repo_root: Path | None = None, now: datetime | None = None) -> str:
    """Open or bring forward the issue's talk terminal; return the line to print on stdout.

    An active run gets a live-mode terminal (its log), any other run a resume-mode one,
    chosen from the run record. A running issue's ``--message`` is delivered first (Codex
    queue or Claude hint), before any lock; a failed Codex send opens and writes nothing.
    The decision (read the derived talk, probe it, re-show, unknown exit or launch, append
    the event) then runs under ``worktree_lock(n)``, the lock the tick's stall and runtime
    stop also take, with the opener lock inside it.
    """
    language = config.language
    root = (repo_root or Path.cwd()).resolve()
    run = store.get_run(number)
    if run is None:
        raise TalkError(text("talk_no_session", language, n=number))
    executor, session = _session(run, config)
    running = run.status == "running"
    if session is None and not (running and executor == "claude"):
        raise TalkError(text("talk_no_session", language, n=number))
    sent = _deliver(store, run, number, executor, message, root, language) if running and message else ""
    try:
        with store.worktree_lock(number, timeout=TALK_LOCK_WAIT), orch_spawn.watch_open._opening(store):
            return _decide(number, message, store, config, root, now, sent) or sent
    except LockBusy as exc:
        raise TalkError(text("talk_busy", language, n=number), stdout=sent) from exc


def _decide(number: int, message: str | None, store: StateStore, config: Config, root: Path,
            now: datetime | None, sent: str) -> str:
    """Under the worktree and opener locks: re-show, report unknown, or launch and append the event."""
    language = config.language
    run = store.get_run(number)  # Read again under the lock: the run may have finished meanwhile.
    executor, session = _session(run, config)
    running = run.status == "running"
    mode = "live" if running else "resume"
    if session is None and not (running and executor == "claude"):
        raise TalkError(text("talk_no_session", language, n=number), stdout=sent)
    worktree = store.worktree_path(number)
    cwd = worktree if worktree.is_dir() else root
    # A unique title lets a later probe find a terminal whose handle was never saved.
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
    if running:
        current = store.talk_state(run)[0]
    else:
        # Only resume-mode events of the issue's latest run; a --message always opens a new one.
        resumes = store.resume_events(run)
        current = resumes[-1] if resumes and not message else None
    target = probe.resolved(current, cwd) if current is not None else None
    state = None if current is None else probe.liveness(target, cwd) if target is not None else probe.UNKNOWN
    if running and state == probe.UNKNOWN:
        # It may still be open: bring nothing forward and never open a second terminal beside it.
        raise TalkError(text("talk_unknown", language, n=number), stdout=sent)
    if state == probe.OPEN:
        if not probe.bring_forward(target, cwd):
            raise TalkError(text("talk_bring_failed", language, n=number), stdout=sent)
        if target is not current:  # A pending terminal found by title: save its handle now.
            found = {"kind": "tmux", "socket": current.get("socket")} if "socket" in current else {"kind": "orca"}
            _opened(store, run, number, executor, mode, found, current.get("title", title),
                    target.get("window_id") or target.get("handle"), now)
        return sent or text("talk_reshown", language, n=number, screen="tmux" if "socket" in current else "orca")
    opener = _opener(store, root)
    if opener is None:
        return " ".join(filter(None, [sent, text("talk_manual", language, command=f"cd {shlex.quote(str(cwd))} && "
                                                  + shlex.join(command))]))
    trackable = opener["kind"] in {"orca", "tmux"}
    try:
        handle = _launch(opener, cwd, title, command)
    except subprocess.SubprocessError as exc:
        # The terminal may have opened anyway: a pending event keeps the talk until a probe finds it.
        if trackable:
            _opened(store, run, number, executor, mode, opener, title, None, now)
        raise TalkError(text("talk_open_unknown", language, n=number), stdout=sent) from exc
    if trackable:
        _opened(store, run, number, executor, mode, opener, title, handle, now)
    opened = text("talk_log_opened" if running else "talk_resume_opened", language, n=number, screen=opener["kind"])
    parts = [sent or opened]
    if running and not trackable:
        parts.append(text("talk_no_handle", language))
    return " ".join(parts)


def _deliver(store: StateStore, run: Run, number: int, executor: str, message: str, cwd: Path, language: str) -> str:
    if executor == "codex":
        # The session is read from the run record now, at send time: never an earlier copy.
        _queue(_current_session(store, run, number, language), message, cwd, number, language)
        return text("talk_sent", language, n=number)
    _save_hint(store, number, message)
    return text("talk_hint_saved", language, n=number)


def _opened(store: StateStore, run: Run, number: int, executor: str, mode: str, opener: dict, title: str,
            handle: str | None, now: datetime | None) -> None:
    """Append the one talk-opened event: its mode, and handle, socket and window id, or the pending title."""
    fields: dict = {"run_started_at": run.started_at, "executor": executor, "mode": mode}
    if opener["kind"] == "tmux":
        fields["socket"] = opener["socket"]
    if handle and opener["kind"] == "orca":
        fields["handle"] = handle
    elif handle:
        fields["window_id"] = handle
    else:
        fields["title"] = title
    append_event(store, _now(store, now), number, TALK_OPENED, f"Talk opened for #{number} ({executor})", **fields)


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

