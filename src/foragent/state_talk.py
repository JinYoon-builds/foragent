"""Talk state derived from append-only talk events, and the union pause credit (D55 addendum, D95).

`fa lanes talk` appends ``talk-opened`` events to the orch events journal and never
writes the run record. A run's current talk is its latest ``talk-opened`` event (for
its ``run_started_at``) with no matching ``talk-closed`` event; ``Run.talking`` and
``talking_since`` are derived from it on every read and never stored. Only the gate
tick (and ``finish_run``) writes: one ``talk-closed`` event and the paused-time credit,
through ``add_waited``, the helper every pause credit goes through.
"""

from datetime import datetime
from itertools import pairwise
import json
from typing import Any

from foragent.state_records import Run

TALK_OPENED, TALK_CLOSED = "talk-opened", "talk-closed"
# A talk-closed event names the talk-opened event it ends by these fields.
IDENTITY = ("run_started_at", "handle", "socket", "window_id", "title")


def talk_identity(event: dict) -> tuple:
    return tuple(event.get(name) for name in IDENTITY)


def _minutes(seconds: float) -> str:
    return "<1 min" if seconds < 60 else f"{int(seconds // 60)} min"


class TalkRecordsMixin:
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

    def talk_events(self, number: int) -> list[dict]:
        """This issue's talk events in journal order; the scan is cached by the file's size and mtime."""
        path = self.root / "orch-events.jsonl"
        try:
            stat = path.stat()
        except FileNotFoundError:
            return []
        key = (stat.st_size, stat.st_mtime_ns)
        cached = getattr(self, "_talk_cache", None)
        if cached is None or cached[0] != key:
            by_issue: dict[int, list[dict]] = {}
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("kind") in (TALK_OPENED, TALK_CLOSED):
                        by_issue.setdefault(event.get("issue"), []).append(event)
            cached = (key, by_issue)
            self._talk_cache = cached
        return list(cached[1].get(number, []))

    def talk_state(self, run: Run) -> tuple[dict | None, list[tuple[dict, str]]]:
        """``(current talk-opened event or None, [(superseded open event, its closing time)])``."""
        events = self.talk_events(run.number)
        closed = {talk_identity(event) for event in events if event["kind"] == TALK_CLOSED}
        opened = [event for event in events
                  if event["kind"] == TALK_OPENED and event.get("run_started_at") == run.started_at]
        superseded = [(event, newer["at"]) for event, newer in pairwise(opened)
                      if talk_identity(event) not in closed]
        current = opened[-1] if opened and talk_identity(opened[-1]) not in closed else None
        return current, superseded

    def talk_closed_at(self, run: Run) -> str | None:
        """When this run's latest talk closed; the stall clock restarts there."""
        times = [event["at"] for event in self.talk_events(run.number)
                 if event["kind"] == TALK_CLOSED and event.get("run_started_at") == run.started_at]
        return max(times) if times else None

    def _with_talk(self, run: Run | None) -> Run | None:
        """The run with its derived talk; a finished run never talks."""
        if run is not None and run.status == "running":
            current, _ = self.talk_state(run)
            if current is not None:
                object.__setattr__(run, "_talk_since", current["at"])
        return run

    def open_talk_pause(self, number: int, event: dict, now: datetime) -> None:
        """The tick marks a current talk's pause open in the union, once (keyed by the event's time)."""
        with self._state_lock():
            run = self.get_run(number)
            if run is None or run.status != "running" or event["at"] in run.pauses_open:
                return
            at = _state.datetime.fromisoformat(event["at"])
            mark = _state.datetime.fromisoformat(run.pause_mark) if run.pause_mark else at
            # The union only moves forward: a talk seen after another pause began opens from there.
            self._write_run(_state.replace(run, **_pause_event(run, max(at, mark), 0.0,
                                                               "open", event["at"])))

    def close_talk(self, number: int, event: dict, closed_at: datetime) -> Run | None:
        """The tick closes one talk: one talk-closed event and its paused time credited once."""
        with self._state_lock():
            run = self.get_run(number)
            if run is None or run.status != "running":
                return run
            ended = self._talk_ended(run, event, closed_at)
            if ended is not run:
                self._write_run(ended)
            return ended

    def _talk_ended(self, run: Run, event: dict, closed_at: datetime) -> Run:
        """Under the state lock: append talk-closed and credit, unless the talk is already closed."""
        from foragent.orch_events import append_event

        if any(other["kind"] == TALK_CLOSED and talk_identity(other) == talk_identity(event)
               for other in self.talk_events(run.number)):
            return run
        length = max(0.0, (closed_at - _state.datetime.fromisoformat(event["at"])).total_seconds())
        fields = {name: event[name] for name in IDENTITY if name in event}
        append_event(self, closed_at, run.number, TALK_CLOSED,
                     f"Talk closed for #{run.number} after {_minutes(length)}", locked=True,
                     **{"executor": event.get("executor"), **fields})
        return _state.replace(run, **_pause_event(run, closed_at, length, "close", event["at"]))

    def _finish_talk(self, run: Run) -> Run:
        """``finish_run`` (under the state lock): an open talk ends with the run, credited once."""
        current, superseded = self.talk_state(run)
        for event, newer_at in superseded:
            run = self._talk_ended(run, event, _state.datetime.fromisoformat(newer_at))
        if current is not None:
            run = self._talk_ended(run, current, self._now())
        return run


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
