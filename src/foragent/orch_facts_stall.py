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


def supply(config: Config, running: list[Run], work: dict[str, Any]) -> dict[str, Any]:
    """Free lanes and the waiting issues the admission replay does not hold.

    ``work`` is `fa orch facts`' one admission replay (``resources.parallelizable_work``, from
    ``orch_facts_resources._admission``): startable is its ``waiting_issues`` minus its ``held``,
    the same answer facts prints under held, with no second count. A free lane has no live run.
    ``short``: fewer startable issues than free lanes.
    """
    free = sorted((set(config.lanes) | {"main"}) - {run.lane or "main" for run in running})
    held = set(work.get("held") or {})
    startable = [number for number in work.get("waiting_issues") or [] if str(number) not in held]
    return {"free_lanes": free, "startable": len(startable), "short": len(startable) < len(free)}


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


def hold(store: StateStore, current: dict[str, Any] | None, now: datetime) -> None:
    """The gate tick's record of when supply went low, from the facts' ``supply`` it just measured;
    removed once supply covers the free lanes. The facts read it next tick (``supply_low_numbers``)."""
    path = store.root / HOLD_FILE
    with store._state_lock():
        if not (current or {}).get("short"):
            path.unlink(missing_ok=True)
        elif _since(store) is None:
            store._write_json(path, {"since": now.isoformat()})


def supply_low_numbers(store: StateStore, now: datetime, current: dict[str, Any]) -> dict[str, Any]:
    """Free lanes (by name), their count, startable approved issues and issues waiting only for approval.

    Present once supply has been short since the tick's hold for ``SUPPLY_HOLD`` and still is."""
    since = _since(store)
    if since is None or now - since < SUPPLY_HOLD or not current.get("short"):
        return {}
    return {"free_lanes": current["free_lanes"], "free_slots": len(current["free_lanes"]),
            "startable": current["startable"], "unapproved_ready": _unapproved_ready(store)}


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


# GitHub's ways to name an issue or PR in text: `#N`, `owner/name#N` and an issue or PR URL.
_REFERENCE = re.compile(
    r"(?:https?://github\.com/(?P<url>[\w.-]+/[\w.-]+)/(?:issues|pull)/(?P<url_n>\d+)"
    r"|(?<![\w/.-])(?P<slug>[\w.-]+/[\w.-]+)#(?P<slug_n>\d+)"
    r"|(?<![\w/.-])#(?P<bare_n>\d+))\b")


def references(text: str, repo: str | None) -> list[int]:
    """This repository's issue and PR numbers a text names, in order: a bare `#N`, and `owner/name#N`
    or a GitHub URL whose owner/name is ``repo`` (any case). Another repository's references are left out."""
    own = (repo or "").casefold()
    found = []
    for match in _REFERENCE.finditer(text):
        if match["bare_n"]:
            found.append(int(match["bare_n"]))
        elif (slug := match["slug"] or match["url"]) and own and slug.casefold() == own:
            found.append(int(match["slug_n"] or match["url_n"]))
    return found


def _resolved(store: StateStore, rows: list[dict[str, Any]]) -> set[int]:
    """Numbers whose work is over, from what the gate records: issues now closed, and PRs the gate
    merged (an issue row's own branch PR marked merged, a merge owner fact, or a gate-branch PR in
    the issue cache). A PR row itself is not merge evidence."""
    resolved = {row["number"] for row in rows if row.get("state") == "closed" and not row.get("is_pull_request")}
    resolved |= {row["pr_number"] for row in rows if row.get("merged") and type(row.get("pr_number")) is int}
    resolved |= {fact["pr"] for fact in store.owner_merge_facts().values() if type(fact.get("pr")) is int}
    for row in rows:
        resolved.update(int(pr) for pr, merge in (row.get("merges") or {}).items() if merge.get("merged_at"))
    return resolved


def park_resolved_numbers(store: StateStore, repo: str | None = None) -> dict[str, Any]:
    """Parked issues whose park reason names an issue or gate PR whose work is over: parked -> those.

    ``repo`` (owner/name) lets a qualified reference or URL to this repository count too."""
    rows = store.local_issues()
    resolved = _resolved(store, rows)
    found = {}
    for row in rows:
        if row.get("state") != "open" or row.get("is_pull_request"):
            continue
        reason = _park_reason(store, row)
        if reason is None:
            continue
        named = [n for n in references(reason.partition("\n")[2], repo) if n != row["number"]]
        if done := [n for n in dict.fromkeys(named) if n in resolved]:
            found[f"#{row['number']}"] = [f"#{n}" for n in done]
    return {"parked": found} if found else {}


def collect(store: StateStore, now: datetime, current: dict[str, Any], repo: str | None = None) -> dict[str, Any]:
    return {"supply_low": supply_low_numbers(store, now, current),
            "request_stuck": request_stuck_numbers(store, now),
            "park_resolved": park_resolved_numbers(store, repo)}
