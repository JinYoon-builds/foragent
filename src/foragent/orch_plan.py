"""The per-repository plan under ``<state>/plan.json``.

One file holds the lane queues, plan pauses (until, why), lane widths and the
plan's expiry, each with the author who wrote it (``human`` or
``orchestrator``). The plan is a preference above the safety lines: the tick
still applies blockers, budgets, the overlap cap and exclusive rules, and a
queue never unpauses a lane.

A pause lapses when its ``until`` passes; the tick drops it without an event.
When ``expires_at`` passes, the whole plan lapses: the first tick (or edit)
after it rewrites the file to an empty plan and writes one ``plan-expired``
event. An orchestrator never changes or clears an item a person wrote.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from foragent import orch_events
from foragent.state import StateStore

AUTHORS = ("human", "orchestrator")


class PlanRefused(Exception):
    """An edit the plan rules refuse; nothing was changed."""


def _path(store: StateStore) -> Path:
    return store.root / "plan.json"


def _empty() -> dict:
    return {"queues": {}, "paused": {}, "width": {}, "expires_at": None,
            "authors": {"queues": {}, "width": {}, "expires_at": None}}


def _read(store: StateStore) -> dict:
    """The stored plan in canonical form; the caller holds the state lock."""
    plan = _empty()
    path = _path(store)
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        authors = data.get("authors") or {}
        plan["queues"] = {str(lane): [int(n) for n in numbers] for lane, numbers in (data.get("queues") or {}).items()}
        plan["paused"] = {str(lane): {"until": item["until"], "why": item.get("why", ""), "by": item.get("by", "human")}
                          for lane, item in (data.get("paused") or {}).items()}
        plan["width"] = {str(lane): int(n) for lane, n in (data.get("width") or {}).items()}
        plan["expires_at"] = data.get("expires_at")
        plan["authors"] = {
            "queues": {lane: (authors.get("queues") or {}).get(lane, "human") for lane in plan["queues"]},
            "width": {lane: (authors.get("width") or {}).get(lane, "human") for lane in plan["width"]},
            "expires_at": (authors.get("expires_at") or "human") if plan["expires_at"] else None,
        }
    return plan


def _migrate(store: StateStore, plan: dict) -> bool:
    """Move a pre-plan ``lane-queues.json`` into the plan once (written by a person)."""
    legacy = store.root / "lane-queues.json"
    if not legacy.is_file():
        return False
    data = json.loads(legacy.read_text(encoding="utf-8"))
    for lane, numbers in data.items():
        order = list(dict.fromkeys(int(n) for n in numbers))
        if order and str(lane) not in plan["queues"]:
            plan["queues"][str(lane)] = order
            plan["authors"]["queues"][str(lane)] = "human"
    _write(store, plan)
    legacy.unlink()
    return True


def _write(store: StateStore, plan: dict) -> None:
    store._write_json(_path(store), _stored(plan))


def _stored(plan: dict) -> dict:
    view = deepcopy(plan)
    view["written_by"] = written_by(plan)
    return view


def written_by(plan: dict) -> str | None:
    """``human`` when any live item is a person's, else ``orchestrator``; None when empty."""
    authors = [*plan["authors"]["queues"].values(), *plan["authors"]["width"].values(),
               *(item["by"] for item in plan["paused"].values())]
    if plan["expires_at"]:
        authors.append(plan["authors"]["expires_at"])
    if not authors:
        return None
    return "human" if "human" in authors else "orchestrator"


def _expired(plan: dict, now: datetime) -> bool:
    return bool(plan["expires_at"]) and now >= datetime.fromisoformat(plan["expires_at"])


def _drop_lapsed(plan: dict, now: datetime) -> bool:
    lapsed = [lane for lane, item in plan["paused"].items() if now >= datetime.fromisoformat(item["until"])]
    for lane in lapsed:
        del plan["paused"][lane]
    return bool(lapsed)


def _bring_up_to_date(store: StateStore, now: datetime) -> tuple[dict, list]:
    """Migrate, expire and drop lapsed pauses; the caller holds the state lock.

    Returns the live plan and the events to append once the lock is released
    (the journal takes the same, non-reentrant lock).
    """
    plan = _read(store)
    _migrate(store, plan)
    if _expired(plan, now):
        expired_at = plan["expires_at"]
        _write(store, _empty())
        return _empty(), [("plan-expired", None, f"plan expired at {expired_at}; defaults apply",
                           {"expires_at": expired_at, "by": None})]
    if _drop_lapsed(plan, now):
        _write(store, plan)
    return plan, []


def _journal(store: StateStore, now: datetime, events: list) -> None:
    for kind, lane, summary, details in events:
        orch_events.append_event(store, now, None, kind, summary, details={"lane": lane, **details})


def load(store: StateStore, now: datetime | None = None) -> dict:
    """The plan in force, read only (apart from the one-time queue migration)."""
    now = now or store._now()
    if (store.root / "lane-queues.json").is_file():
        with store._state_lock():
            _migrate(store, _read(store))
    plan = _read(store)
    if _expired(plan, now):
        return _empty()
    _drop_lapsed(plan, now)
    return plan


def refresh(store: StateStore, now: datetime) -> dict:
    """Once per tick: drop lapsed pauses silently and expire the plan with one event."""
    with store._state_lock():
        plan, events = _bring_up_to_date(store, now)
    _journal(store, now, events)
    return plan


def view(plan: dict) -> dict:
    """The ``fa orch plan --json`` shape."""
    return _stored(plan)


def queue(store: StateStore, lane: str, now: datetime | None = None) -> list[int]:
    return load(store, now)["queues"].get(lane, [])


def width(store: StateStore, lane: str, now: datetime) -> int | None:
    return load(store, now)["width"].get(lane)


def lane_paused(store: StateStore, lane: str, now: datetime) -> bool:
    return lane in load(store, now)["paused"]


def _check_author(owner: str | None, by: str, what: str) -> None:
    if by == "orchestrator" and owner == "human":
        raise PlanRefused(f"{what} was written by a human; the orchestrator cannot change it")


def _edit(store: StateStore, by: str, change) -> dict:
    """Apply ``change(plan, now)`` all or nothing; it returns events or raises PlanRefused."""
    if by not in AUTHORS:
        raise PlanRefused(f"--by must be one of: {', '.join(AUTHORS)}")
    now = store._now()
    with store._state_lock():
        plan, events = _bring_up_to_date(store, now)
        edited = deepcopy(plan)
        changes = change(edited, now)  # Raises before anything below is written.
        _write(store, edited)
    _journal(store, now, events + [(kind, lane, summary, {"by": by, **details})
                                   for kind, lane, summary, details in changes])
    return edited


def set_queue(store: StateStore, lane: str, numbers: list[int], by: str = "human") -> list[int]:
    """Replace the lane's queue; an empty list clears it."""
    order = list(dict.fromkeys(int(n) for n in numbers))

    def change(plan, _now):
        _check_author(plan["authors"]["queues"].get(lane), by, f"lane {lane}'s queue")
        if order:
            plan["queues"][lane] = order
            plan["authors"]["queues"][lane] = by
            summary = f"lane {lane} queue {' '.join(f'#{n}' for n in order)} by {by}"
        else:
            plan["queues"].pop(lane, None)
            plan["authors"]["queues"].pop(lane, None)
            summary = f"lane {lane} queue cleared by {by}"
        return [("plan-queue", lane, summary, {"order": order})]

    _edit(store, by, change)
    return order


def pause(store: StateStore, lane: str, until: datetime, why: str, by: str = "human") -> dict:
    def change(plan, now):
        if until <= now:
            raise PlanRefused(f"--until {until.isoformat()} is in the past")
        current = plan["paused"].get(lane)
        _check_author(current and current["by"], by, f"lane {lane}'s pause")
        plan["paused"][lane] = {"until": until.isoformat(), "why": why, "by": by}
        summary = f"lane {lane} paused until {until.isoformat()} by {by}: {why}"
        return [("plan-pause", lane, summary, {"until": until.isoformat(), "why": why})]

    return _edit(store, by, change)


def resume(store: StateStore, lane: str, by: str = "human") -> bool:
    """Remove the lane's plan pause; False when the lane had none."""
    removed = []

    def change(plan, _now):
        current = plan["paused"].get(lane)
        if current is None:
            return []
        _check_author(current["by"], by, f"lane {lane}'s pause")
        del plan["paused"][lane]
        removed.append(lane)
        return [("plan-resume", lane, f"lane {lane} resumed by {by}", {})]

    _edit(store, by, change)
    return bool(removed)


def set_plan(store: StateStore, widths: dict[str, int | None], expires: datetime | None,
             by: str = "human") -> dict:
    """Set lane widths (None clears one) and the expiry together; all or nothing."""
    def change(plan, now):
        for lane in widths:
            _check_author(plan["authors"]["width"].get(lane), by, f"lane {lane}'s width")
        if expires is not None:
            _check_author(plan["authors"]["expires_at"], by, "the plan's expiry")
            if expires <= now:
                raise PlanRefused(f"--expires {expires.isoformat()} is in the past")
        events = []
        for lane, value in widths.items():
            if value is None:
                plan["width"].pop(lane, None)
                plan["authors"]["width"].pop(lane, None)
                summary = f"lane {lane} width cleared by {by}"
            else:
                plan["width"][lane] = value
                plan["authors"]["width"][lane] = by
                summary = f"lane {lane} width {value} by {by}"
            events.append(("plan-width", lane, summary, {"width": value}))
        if expires is not None:
            plan["expires_at"] = expires.isoformat()
            plan["authors"]["expires_at"] = by
            events.append(("plan-expires", None, f"plan expires at {expires.isoformat()} by {by}",
                           {"expires_at": expires.isoformat()}))
        return events

    return _edit(store, by, change)


def parse_time(value: str) -> datetime:
    """An ISO time; one without a timezone is local time."""
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone() if parsed.tzinfo is None else parsed
