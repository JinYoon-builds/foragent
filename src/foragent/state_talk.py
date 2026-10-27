"""Talk state on run records: begin, publish, end and the waited-time credit (D55 addendum)."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from foragent.state_records import Run


class TalkRecordsMixin:
    def _drop_talk_record(self, number: int) -> None:
        (self.root / "talk" / f"{number}.json").unlink(missing_ok=True)

    @staticmethod
    def _talk_ended(run: Run, now: datetime, *, restart_stall: bool) -> Run:
        """The one accounting for every talk ending (``finish_run`` uses it too)."""
        return _talk_ended(run, now, restart_stall=restart_stall)

    @contextmanager
    def talk_lock(self, number: int, *, blocking: bool = True) -> Iterator[bool]:
        """Serialize an issue's talk lifecycle with session changes; yields False when busy and not blocking.

        Lock order: talk -> worktree -> state. `fa lanes talk` holds it while it
        opens a terminal and sends; the runner holds it while it records a new
        session; the tick only tries it.
        """
        path = self.root / "talk" / f"{number}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            try:
                _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_EX | (0 if blocking else _state.fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                _state.fcntl.flock(stream.fileno(), _state.fcntl.LOCK_UN)

    def add_waited(self, number: int, seconds: float, *, wait: str | None = None,
                   key: str | None = None, now: datetime | None = None, **fields: Any) -> Run:
        """The one helper every pause credit goes through: ``waited_seconds`` stays the union.

        ``seconds`` (negative corrects an advance credit) is the caller's own measure
        of its pause. ``wait="open"`` / ``"close"`` with a ``key`` marks that pause's span
        at ``now``. Time when several pauses were open is counted once, so concurrent
        backoffs, a test-slot queue and a talk add only the length of their union.
        """
        with self._state_lock():
            run = self.get_run(number)
            if run is None:
                raise KeyError(number)
            if run.status == "finished":
                return run
            updated = _state.replace(run, **_pause_event(run, now, seconds, wait, key), **fields)
            self._write_run(updated)
            return updated

    def begin_talk(self, number: int, started_at: str, now: datetime) -> Run | None:
        """Mark the run ``started_at`` as talking before its terminal opens; None if it is no longer running."""
        with self._state_lock():
            run = self.get_run(number)
            if run is None or run.status != "running" or run.started_at != started_at:
                return None
            if run.talking:
                return run
            updated = _state.replace(run, talking=True, talking_since=now.isoformat(),
                                     **_pause_event(run, now, 0.0, "open", "talk"))
            self._write_run(updated)
            return updated

    def publish_talk(self, number: int, started_at: str, status: str, record: dict) -> bool:
        """Save a talk terminal record only while the run is still the same run in the same state."""
        with self._state_lock():
            run = self.get_run(number)
            if run is None or run.started_at != started_at or run.status != status:
                return False
            if status == "running" and not run.talking:
                return False  # The talk already ended (or never began); nothing may pause.
            self._write_json(self.root / "talk" / f"{number}.json", record)
            return True

    def end_talk(self, number: int, now: datetime, *, restart_stall: bool = True) -> Run | None:
        """End a talk: credit its paused time, drop its record and, if asked, restart the stall clock at ``now``."""
        with self._state_lock():
            run = self.get_run(number)
            if run is None or run.status == "finished" or not run.talking:
                return run
            updated = _talk_ended(run, now, restart_stall=restart_stall)
            self._write_run(updated)
            (self.root / "talk" / f"{number}.json").unlink(missing_ok=True)
            return updated


def _talk_ended(run: Run, now: datetime, *, restart_stall: bool) -> Run:
    """The one accounting for every talk ending: credit the paused seconds, clear talking."""
    if not run.talking:
        return run
    fields = {"last_progress_at": now.isoformat()} if restart_stall else {}
    since = _state.datetime.fromisoformat(run.pauses_open.get("talk") or run.talking_since or now.isoformat())
    length = max(0.0, (now - since).total_seconds())
    # The talk's whole length, through the shared union accounting.
    return _state.replace(run, talking=False, talking_since=None,
                          **_pause_event(run, now, length, "close", "talk"), **fields)


def _pause_event(run: Run, now: datetime | None, seconds: float, wait: str | None, key: str | None) -> dict:
    """Fields after crediting ``seconds`` and opening or closing pause ``key`` at ``now``.

    Before the change, the time since ``pause_mark`` with k pauses open was counted
    k times by their own credits; (k - 1) times that is taken back, so the total is
    the union of the pause intervals.
    """
    opened = dict(run.pauses_open)
    waited = run.waited_seconds + seconds
    if now is None:
        return {"waited_seconds": waited}
    waited -= run.pending_overlap(now)
    if wait == "open" and key is not None:
        opened.setdefault(key, now.isoformat())
    elif wait == "close" and key is not None:
        opened.pop(key, None)
    return {"waited_seconds": waited, "pauses_open": opened, "pause_mark": now.isoformat() if opened else None}


import foragent.state as _state
