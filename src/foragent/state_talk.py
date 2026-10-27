"""Talk state derived from append-only talk events, and the union pause credit (D55 addendum, D95).

`fa lanes talk` appends ``talk-opened`` events to the orch events journal and never
writes the run record. A run's current talk is its latest ``talk-opened`` event (for
its ``run_started_at``) with no matching ``talk-closed`` event; ``Run.talking`` and
``talking_since`` are derived from it on every read and never stored. The gate tick
(and ``finish_run``) closes talks with one ``talk-closed`` event. The talk's paused time
is derived from the events too (``Run.talk_pause_seconds``): only runner waits credit
``waited_seconds`` (as wall-clock regions, by their union), and ``finish_run`` folds the
talk's share in once, in the same write that finishes the run.
"""

from datetime import datetime
from itertools import pairwise
import json
from typing import Any

from foragent.state_records import Run, union_seconds

TALK_OPENED, TALK_CLOSED = "talk-opened", "talk-closed"
# A talk-closed event names the talk-opened event it ends by these fields.
IDENTITY = ("run_started_at", "handle", "socket", "window_id", "title")


def talk_identity(event: dict) -> tuple:
    return tuple(event.get(name) for name in IDENTITY)


def _minutes(seconds: float) -> str:
    return "<1 min" if seconds < 60 else f"{int(seconds // 60)} min"


class TalkRecordsMixin:
    def add_waited(self, number: int, seconds: float = 0.0, *, key: str | None = None,
                   start: datetime | None = None, end: datetime | None = None, close: bool = False,
                   **fields: Any) -> Run:
        """The one helper every pause credit goes through: ``waited_seconds`` stays the union.

        A plain credit adds ``seconds``. A keyed runner wait sets its wall-clock region to
        ``start``..``end`` (its own measure: it may grow or, to correct an advance credit,
        shrink), and ``close`` merges it into the closed regions. ``waited_seconds`` moves by
        exactly the change in the length of the regions' union, so overlapping waits count
        once, whichever order they credit in.
        """
        with self._state_lock():
            run = self.get_run(number)
            if run is None:
                raise KeyError(number)
            if run.status == "finished":
                return run
            if key is not None:
                before = union_seconds(run.wait_regions())
                regions, closed = dict(run.pause_regions), [list(pair) for pair in run.pause_closed]
                if start is not None and end is not None:
                    regions[key] = [start.isoformat(), max(start, end).isoformat()]
                if close and key in regions:
                    closed = _merged([*closed, regions.pop(key)])
                fields.update(pause_regions=regions, pause_closed=closed)
                probe = _state.replace(run, pause_regions=regions, pause_closed=closed)
                seconds += union_seconds(probe.wait_regions()) - before
            updated = _state.replace(run, waited_seconds=run.waited_seconds + seconds, **fields)
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

    def talk_regions(self, run: Run) -> list[tuple[datetime, datetime | None]]:
        """Each talk of this run as (start, end): closed at its talk-closed event, superseded at the
        next talk-opened event, or still open (None)."""
        events = self.talk_events(run.number)
        closed = {talk_identity(event): event["at"] for event in events if event["kind"] == TALK_CLOSED}
        opened = [event for event in events
                  if event["kind"] == TALK_OPENED and event.get("run_started_at") == run.started_at]
        regions = []
        for index, event in enumerate(opened):
            end = closed.get(talk_identity(event)) or (opened[index + 1]["at"] if index + 1 < len(opened) else None)
            regions.append((_state.datetime.fromisoformat(event["at"]),
                            _state.datetime.fromisoformat(end) if end else None))
        return regions

    def _with_talk(self, run: Run | None) -> Run | None:
        """The run with its derived talk and talk regions; a finished run never talks."""
        if run is not None and run.status == "running":
            current, _ = self.talk_state(run)
            if current is not None:
                object.__setattr__(run, "_talk_since", current["at"])
            object.__setattr__(run, "_talk_regions", self.talk_regions(run))
        return run

    def close_talk(self, number: int, event: dict, closed_at: datetime) -> Run | None:
        """The tick closes one talk: its region credited through the union, then one talk-closed event.

        The credit is written first. If the event then fails, the talk still reads as open,
        the next tick closes it again, and re-crediting the same region adds nothing.
        """
        with self._state_lock():
            run = self.get_run(number)
            if run is None or run.status != "running" or self._closed(run, event):
                return run
            credited = _credit_region(run, _state.datetime.fromisoformat(event["at"]), closed_at)
            self._write_run(credited)
            self._append_closed(run, event, closed_at)
            return self.get_run(number)

    def _closed(self, run: Run, event: dict) -> bool:
        return any(other["kind"] == TALK_CLOSED and talk_identity(other) == talk_identity(event)
                   for other in self.talk_events(run.number))

    def _append_closed(self, run: Run, event: dict, closed_at: datetime) -> None:
        """Under the state lock: one talk-closed event, unless that talk is already closed."""
        from foragent.orch_events import append_event

        if self._closed(run, event):
            return
        length = max(0.0, (closed_at - _state.datetime.fromisoformat(event["at"])).total_seconds())
        fields = {name: event[name] for name in IDENTITY if name in event}
        append_event(self, closed_at, run.number, TALK_CLOSED,
                     f"Talk closed for #{run.number} after {_minutes(length)}", locked=True,
                     **{"executor": event.get("executor"), **fields})

    def _finish_talk(self, run: Run) -> Run:
        """``finish_run`` (under the state lock): every talk region of the run credited once.

        Credit goes into the run write that finishes the run; the closing events are
        appended only after that write (``_flush_talk_closes``), so an event never exists
        without its credit. Re-crediting a region another tick already credited adds nothing.
        """
        now = self._now()
        for start, end in self.talk_regions(run):
            run = _credit_region(run, start, end or now)
        current, superseded = self.talk_state(run)
        closes = [(event, _state.datetime.fromisoformat(newer_at)) for event, newer_at in superseded]
        self._talk_closes = [*closes, *([(current, now)] if current is not None else [])]
        return run

    def _flush_talk_closes(self, run: Run) -> None:
        """After ``finish_run``'s write: the talk-closed events for the talks it credited."""
        for event, closed_at in getattr(self, "_talk_closes", []):
            self._append_closed(run, event, closed_at)
        self._talk_closes = []


def _credit_region(run: Run, start: datetime, end: datetime) -> Run:
    """The run with one more closed pause region; ``waited_seconds`` grows by the union's growth."""
    before = union_seconds(run.wait_regions())
    closed = _merged([*(list(pair) for pair in run.pause_closed), [start.isoformat(), max(start, end).isoformat()]])
    credited = _state.replace(run, pause_closed=closed)
    return _state.replace(credited, waited_seconds=run.waited_seconds + union_seconds(credited.wait_regions()) - before)


def _merged(pairs: list[list[str]]) -> list[list[str]]:
    """Closed regions merged where they overlap, so the list stays short."""
    merged: list[list[str]] = []
    for start, end in sorted(pairs, key=lambda pair: _state.datetime.fromisoformat(pair[0])):
        if merged and _state.datetime.fromisoformat(start) <= _state.datetime.fromisoformat(merged[-1][1]):
            if _state.datetime.fromisoformat(end) > _state.datetime.fromisoformat(merged[-1][1]):
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


import foragent.state as _state
