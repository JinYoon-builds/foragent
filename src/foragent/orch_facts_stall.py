"""Flow-stall kinds of `fa orch facts` (#2088): the gate tells the orchestrator the flow stalled.

Three kinds, read from records the gate already keeps:

- ``supply_low``: fewer startable approved issues than free lane slots, for ``SUPPLY_HOLD``.
- ``request_stuck``: an inbox request still ``processing`` ``STUCK_AFTER`` after it got there.
- ``park_resolved``: a parked issue whose park reason names an issue that has since closed.

Only the 30-minute hold needs a record of its own: ``HOLD_FILE`` in the store root holds when
supply first went low. Its one writer is the gate tick (``hold``, called before the role-signal
check); facts only read it. It is removed as soon as supply recovers, so a short dip never counts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from types import SimpleNamespace
from typing import Any

from foragent import orch_inbox, orch_park
from foragent.config import Config
from foragent.issue import parse_body
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, WAITING_PERSON
from foragent.orch_facts_resources import _time
from foragent.state import Run, StateStore

SUPPLY_HOLD = timedelta(minutes=30)
STUCK_AFTER = timedelta(hours=3)
HOLD_FILE = "flow-stall.json"
KINDS = ("supply_low", "request_stuck", "park_resolved")
HELD = {BLOCKED, NEEDS_HUMAN, WAITING_PERSON}


def _ctx(store: StateStore, config: Config, root, now: datetime) -> SimpleNamespace:
    return SimpleNamespace(store=store, config=config, repo_root=root, now=lambda: now)


def free_lanes(store: StateStore, config: Config, root, now: datetime, running: list[Run]) -> list[str]:
    """Lanes (with main) the gate could start in now: no live run and no lane hold.

    A lane the plan paused, that dreams, or that quota holds waits on that hold, not on supply
    (``scheduler_start._lane_hold``, the tick's own rule)."""
    from foragent.scheduler_start import _lane_hold

    busy = {run.lane or "main" for run in running}
    ctx = _ctx(store, config, root, now)
    return sorted(lane for lane in (set(config.lanes) | {"main"}) - busy if _lane_hold(ctx, lane, now) is None)


def startable(store: StateStore, config: Config, root, now: datetime, running: list[Run], waiting) -> list[int]:
    """Approved issues the gate's start loop would admit now, in its own order.

    Each must be ``_Waiting.startable`` (ready, not blocked or awaiting a person) and pass the
    gate's file rule (``overlap_files.admits``: lane serialisation, exclusive reservations,
    per-file limits) against the live runs' claims and the claims of those admitted before it,
    as `fa orch facts` replays it (``orch_facts_resources._admission``). Machine slots and lane
    holds are left out: they bind capacity, not supply. Approved work that cannot start therefore
    never covers a free lane.
    """
    from foragent import orch_queue
    from foragent.orch_facts_resources import _simulated_start
    from foragent.overlap_files import admits, claim, claims, recorded_union
    from foragent.scheduler_start import _ranked

    items = {item.number: item for item in waiting if item.startable}
    if not items:
        return []
    ctx = _ctx(store, config, root, now)
    live = {(run.number, run.started_at) for run in running}
    known, unknown = claims(ctx, measured=False)
    known = [entry for entry in known if (entry.run.number, entry.run.started_at) in live]
    unknown = [run for run in unknown if (run.number, run.started_at) in live]
    lanes: dict[str, list[int]] = {}
    for item in items.values():
        lanes.setdefault(item.lane, []).append(item.number)
    ready = {lane: orch_queue.ordered(store, lane, numbers) for lane, numbers in lanes.items()}
    union, admitted = recorded_union(store), []
    for lane, number in _ranked(ctx, ready, claims=list(known)):
        item = items[number]
        if not admits(ctx, lane, item.parsed, known, unknown, union=union):
            continue
        admitted.append(number)
        run = _simulated_start(store, number, lane, now)
        if item.parsed.files:
            known.append(claim(run, item.parsed))
        else:
            unknown.append(run)
    return admitted


def is_low(store: StateStore, config: Config, root, now: datetime, running: list[Run], waiting) -> bool:
    """Startable approved work is below the free lane slots right now."""
    free = free_lanes(store, config, root, now, running)
    return bool(free) and len(startable(store, config, root, now, running, waiting)) < len(free)


def _unapproved_ready(store: StateStore) -> int:
    """Open issues with a lane and a Done when section that wait only for approval.

    Approved means what the gate reads (``_waiting_issues``): the ``approved`` label and an approval
    record. A revoked approval removes the label but may leave its old record, so the label decides.
    Issues held for another reason (parked, blocked, needs-human, waiting-person) wait on that first.
    """
    count = 0
    for row in store.local_issues():
        labels = {str(label) for label in row.get("labels") or ()}
        if row.get("state") != "open" or row.get("is_pull_request") or labels & HELD:
            continue
        if not any(label.startswith("lane:") for label in labels):
            continue
        if APPROVED in labels and store.load_approval(row["number"]) is not None:
            continue
        parsed = parse_body(row.get("body") or "")
        if (parsed.done_when_commands or parsed.done_when_checklist) and not orch_park.is_parked(
                store, row["number"], comments=row.get("comments", [])):
            count += 1
    return count


def _since(store: StateStore) -> datetime | None:
    import json

    try:
        return _time(json.loads((store.root / HOLD_FILE).read_text(encoding="utf-8")).get("since"))
    except (OSError, ValueError, AttributeError):
        return None


def hold(store: StateStore, config: Config, root, now: datetime) -> None:
    """The gate tick's record of when supply went low; removed once supply covers the free lanes."""
    from foragent.orch_facts import _current_runs
    from foragent.orch_facts_resources import _still_running
    from foragent.orch_facts_runs import _waiting_issues

    running = _still_running(store, _current_runs(store))
    low = is_low(store, config, root, now, running, _waiting_issues(store, config, running))
    path = store.root / HOLD_FILE
    with store._state_lock():
        if not low:
            path.unlink(missing_ok=True)
        elif _since(store) is None:
            store._write_json(path, {"since": now.isoformat()})


def supply_low_numbers(store: StateStore, config: Config, root, now: datetime, running: list[Run],
                       waiting) -> dict[str, Any]:
    """Free lanes (by name), their count, startable approved issues and issues waiting only for approval.

    The hold writer and this reader apply the same ``free_lanes`` and ``startable`` rules."""
    since = _since(store)
    if since is None or now - since < SUPPLY_HOLD:
        return {}
    free = free_lanes(store, config, root, now, running)
    admitted = startable(store, config, root, now, running, waiting)
    if not free or len(admitted) >= len(free):
        return {}
    return {"free_lanes": free, "free_slots": len(free), "startable": len(admitted),
            "unapproved_ready": _unapproved_ready(store)}


def processing_since(record: dict[str, Any]) -> datetime | None:
    """When a request still in ``processing`` got there: the first note of its latest unbroken run
    of ``processing`` notes. A repeated ``processing`` (a progress note) does not restart the age;
    only a move through another status does. Without notes, its creation (``updated_at`` moves on
    every annotation)."""
    if record.get("status") != "processing":
        return None
    since = None
    for note in reversed(record.get("notes") or []):
        if note.get("status") != "processing":
            break
        since = note.get("at")
    return _time(since or record.get("created_at"))


def stuck_requests(store: StateStore, now: datetime) -> list[dict[str, Any]]:
    """Requests in ``processing`` for ``STUCK_AFTER`` or longer, oldest first; role signals excluded."""
    from foragent.orch_role_raise import is_signal_request

    rows = []
    for record in orch_inbox.requests(store):
        since = processing_since(record)
        if since is not None and now - since >= STUCK_AFTER and not is_signal_request(record):
            rows.append({"id": record["id"], "text": record.get("text", ""),
                         "minutes": int((now - since).total_seconds() // 60)})
    return rows


def request_stuck_numbers(store: StateStore, now: datetime) -> dict[str, Any]:
    ids = [row["id"] for row in stuck_requests(store, now)]
    return {"requests": ids} if ids else {}


def _park_reason(store: StateStore, row: dict[str, Any]) -> str | None:
    """The text of the issue's current park marker, or None when it is not parked."""
    number = row["number"]
    if not orch_park.is_parked(store, number, comments=row.get("comments", [])):
        return None
    pending = [entry["body"] for entry in store.github_outbox()
               if entry["issue"] == number and entry["action"] == "comment"
               and orch_park._kind(entry["body"], number) == "parked"]
    if pending:
        return pending[-1]
    markers = [(comment["created_at"], comment["id"], comment["body"]) for comment in row.get("comments", [])
               if orch_park._kind(comment["body"], number) == "parked"]
    return max(markers)[2] if markers else None


def park_resolved_numbers(store: StateStore) -> dict[str, Any]:
    """Parked issues whose park reason names another issue that is now closed: parked -> that issue."""
    rows = store.local_issues()
    closed = {row["number"] for row in rows if row.get("state") == "closed"}
    resolved = {}
    for row in rows:
        if row.get("state") != "open" or row.get("is_pull_request"):
            continue
        reason = _park_reason(store, row)
        if reason is None:
            continue
        named = [int(n) for n in re.findall(r"#(\d+)", reason.partition("\n")[2]) if int(n) != row["number"]]
        if done := [n for n in dict.fromkeys(named) if n in closed]:
            resolved[f"#{row['number']}"] = [f"#{n}" for n in done]
    return {"parked": resolved} if resolved else {}


def collect(store: StateStore, config: Config, root, now: datetime, running: list[Run], waiting) -> dict[str, Any]:
    return {"supply_low": supply_low_numbers(store, config, root, now, running, waiting),
            "request_stuck": request_stuck_numbers(store, now),
            "park_resolved": park_resolved_numbers(store)}
