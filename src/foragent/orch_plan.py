"""The per-repository plan under ``<state>/plan.json``.

One file holds the lane queues, plan pauses (until, why), lane widths and the
plan's expiry, each with the author who wrote it (``human`` or
``orchestrator``). It also holds per-issue executor/model assignments and the
goal with its deadline (``orch_assign``); the plan's expiry never lapses those:
an assignment lapses at its own ``until``, and the goal stays until it is
changed or cleared. The plan is a preference above the safety lines: the tick
still applies blockers, budgets, the overlap cap and exclusive rules, and a
queue never unpauses a lane. An orchestrator never changes or clears an item a
person wrote.

One writer (D95): only the ``fa orch`` commands (pause, resume, plan, queue)
write ``plan.json``. Readers derive the plan in force: an expired plan reads as
empty, and a pause whose ``until`` has passed is ignored. The tick appends one
``plan-expired`` event per ``expires_at`` value, deciding from the events
journal. Until a command first writes ``plan.json``, readers take the lane
queues from a pre-plan ``lane-queues.json``; that first write moves them in and
removes the old file.
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
            "authors": {"queues": {}, "width": {}, "expires_at": None},
            "assignments": {}, "goal": None}


# Every key the plan loader reads; anything else (e.g. a throughput goal) is not plan state here.
STORED_KEYS = ("queues", "paused", "width", "expires_at", "authors", "written_by", "assignments", "goal")


def plan_fields(data: dict) -> dict:
    """Only the plan keys of a stored plan, for a reader that must never see other fields."""
    return {key: data[key] for key in STORED_KEYS if key in data}


def _legacy_path(store: StateStore) -> Path:
    return store.root / "lane-queues.json"


def _read(store: StateStore) -> dict:
    """The stored plan in canonical form, or the pre-plan lane queues (a person's) before it exists."""
    plan = _empty()
    path = _path(store)
    if not path.is_file():
        legacy = _legacy_path(store)
        if legacy.is_file():
            for lane, numbers in json.loads(legacy.read_text(encoding="utf-8")).items():
                if order := list(dict.fromkeys(int(n) for n in numbers)):
                    plan["queues"][str(lane)] = order
                    plan["authors"]["queues"][str(lane)] = "human"
        return plan
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
    plan["assignments"] = {str(number): dict(item) for number, item in (data.get("assignments") or {}).items()}
    plan["goal"] = dict(data["goal"]) if data.get("goal") else None
    return plan


def _write(store: StateStore, plan: dict) -> None:
    store._write_json(_path(store), _stored(plan))


def _stored(plan: dict) -> dict:
    view = deepcopy(plan)
    view["written_by"] = written_by(plan)
    return view


def written_by(plan: dict) -> str | None:
    """``human`` when any live item is a person's, else ``orchestrator``; None when empty."""
    authors = [*plan["authors"]["queues"].values(), *plan["authors"]["width"].values(),
               *(item["by"] for item in plan["paused"].values()),
               *(item["by"] for item in plan["assignments"].values())]
    if plan["goal"]:
        authors.append(plan["goal"]["by"])
    if plan["expires_at"]:
        authors.append(plan["authors"]["expires_at"])
    if not authors:
        return None
    return "human" if "human" in authors else "orchestrator"


def _expired(plan: dict, now: datetime) -> bool:
    return bool(plan["expires_at"]) and now >= datetime.fromisoformat(plan["expires_at"])


def _in_force(plan: dict, now: datetime) -> dict:
    """The derived plan: empty once expired; pauses and assignments whose ``until`` has passed left out.

    The plan's expiry lapses lane preferences only; assignments and the goal outlive it.
    """
    if _expired(plan, now):
        plan = {**_empty(), "assignments": plan["assignments"], "goal": plan["goal"]}
    plan["paused"] = {lane: item for lane, item in plan["paused"].items()
                      if now < datetime.fromisoformat(item["until"])}
    plan["assignments"] = {number: item for number, item in plan["assignments"].items()
                           if not item.get("until") or now < datetime.fromisoformat(item["until"])}
    return plan


def _record_expiry(store: StateStore, plan: dict, now: datetime) -> None:
    """Append one ``plan-expired`` per ``expires_at`` value; the caller holds the state lock."""
    if not _expired(plan, now):
        return
    expired_at = plan["expires_at"]
    journal = store.root / "orch-events.jsonl"
    if journal.is_file():
        for line in journal.read_text(encoding="utf-8").splitlines():
            if line.strip() and '"plan-expired"' in line:
                event = json.loads(line)
                if event.get("kind") == "plan-expired" and event.get("expires_at") == expired_at:
                    return
    _journal(store, now, [("plan-expired", None, f"plan expired at {expired_at}; defaults apply",
                           {"expires_at": expired_at, "by": None})])


def _journal(store: StateStore, now: datetime, events: list) -> None:
    """Append events; the caller holds the state lock."""
    for kind, lane, summary, fields in events:
        orch_events.append_event(store, now, None, kind, summary, locked=True, lane=lane, **fields)


def load(store: StateStore, now: datetime | None = None) -> dict:
    """The plan in force at ``now``; reading never writes ``plan.json``."""
    return _in_force(_read(store), now or store._now())


def record_expiry(store: StateStore, now: datetime) -> None:
    """Once per tick: journal an expiry that has passed, once per ``expires_at`` value."""
    if not _path(store).is_file():
        return
    with store._state_lock():
        _record_expiry(store, _read(store), now)


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
        stored = _read(store)
        _record_expiry(store, stored, now)  # The edit replaces an expired plan; its expiry stays recorded.
        edited = _in_force(deepcopy(stored), now)  # An expired plan's next edit starts a fresh plan.
        changes = change(edited, now)  # A refusal raises before the edit is written.
        _write(store, edited)
        _legacy_path(store).unlink(missing_ok=True)  # Its queues are in plan.json now.
        _journal(store, now, [(kind, lane, summary, {"by": by, **fields})
                              for kind, lane, summary, fields in changes])
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
