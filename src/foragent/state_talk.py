"""Talk state on run records: begin, publish, end and the waited-time credit (D55 addendum)."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from foragent.state_records import Run


class TalkRecordsMixin:
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
                   now: datetime | None = None, **fields: Any) -> Run:
        """Add ``seconds`` (negative corrects an advance credit) to ``waited_seconds`` under the state lock.

        Increments keep every concurrent credit. ``wait="open"`` / ``"close"`` marks a
        runner wait's span at ``now``; while a talk is open too, closing it records
        the overlap so the talk's own credit leaves that time out (union, not sum).
        """
        with self._state_lock():
            run = self.get_run(number)
            if run is None:
                raise KeyError(number)
            if run.status == "finished":
                return run
            if wait == "open" and run.wait_open_since is None:
                fields["wait_open_since"] = now.isoformat()
            elif wait == "close":
                fields.update(wait_open_since=None,
                              talk_overlap_seconds=run.talk_overlap_seconds + run.open_overlap(now))
            updated = _state.replace(run, waited_seconds=run.waited_seconds + seconds, **fields)
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
            updated = _state.replace(run, talking=True, talking_since=now.isoformat(), talk_overlap_seconds=0)
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
    # Only the talk time no runner wait already credited: the union of both pauses.
    return _state.replace(run, talking=False, talking_since=None, talk_overlap_seconds=0,
                          waited_seconds=run.waited_seconds + run.talk_pause_seconds(now), **fields)


import foragent.state as _state
