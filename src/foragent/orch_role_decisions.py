"""Role proposal completion and decision reads from the durable inbox (#1336).

Inbox decisions are authoritative; retained legacy proposal events remain readable.
The journal is an audit trail and may be missing or lag a committed inbox write.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from foragent import orch_inbox
from foragent.config import Config
from foragent.orch_facts_resources import _jsonl, _time
from foragent.roles import load_roles
from foragent.state import StateStore


def add_request(store: StateStore, config: Config, source: str, text: str) -> dict[str, Any]:
    """Role sessions deliver proposals with --from <role>; keep that identity on creation."""
    extra = {"role": source} if source in load_roles(config) else None
    return orch_inbox.add_request(store, source, text, extra)


def request_status(store: StateStore, config: Config, request_id: str, status: str,
                   note: str | None, answer: str | None) -> dict[str, Any]:
    roles = load_roles(config)

    def ordinary(record: dict[str, Any]) -> None:
        if status == "done" and (record.get("role") or record.get("from") in roles):
            raise orch_inbox.InboxError("close role proposals with fa orch role-decision")

    return orch_inbox.set_request_status(
        store, request_id, status, note, check=ordinary, answer=answer, require_answer=True,
        empty_answer_reason="Legacy request-status completion: --answer was omitted." if answer is None else None,
    )


def decisions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Latest explicit decision per (role, request); legacy entries without an id stand alone."""
    latest: dict[Any, tuple[tuple, dict[str, Any]]] = {}
    for index, event in enumerate(events):
        if event.get("kind") != "role-proposal" or type(event.get("accepted")) is not bool:
            continue
        request = event.get("request_id")
        key = (event.get("role"), request if request is not None else ("line", index))
        order = (_time(event.get("at")), index)
        if key not in latest or order >= latest[key][0]:
            latest[key] = (order, event)
    return [event for _, event in sorted(latest.values(), key=lambda item: item[0])]


def recorded(store: StateStore, now: datetime) -> list[tuple[datetime, dict[str, Any]]]:
    """Decisions by decision time, with inbox records replacing their audit events.

    Never infer a decision from status, answer prose, or an absent event. New decisions
    carry decided_at in the same write as accepted; no historical records are backfilled.
    """
    rows = []
    for row in orch_inbox.requests(store):
        at = _time(row.get("decided_at"))
        if at is not None and at <= now and row.get("role") and type(row.get("accepted")) is bool:
            rows.append({"at": at.isoformat(), "kind": "role-proposal", "role": row["role"],
                         "request_id": row["id"], "accepted": row["accepted"]})
    authoritative = {(row["role"], row["request_id"]) for row in rows}
    legacy = [event for event in _jsonl(store.root / "orch-events.jsonl")
              if event.get("kind") == "role-proposal"
              and (at := _time(event.get("at"))) is not None and at <= now
              and (event.get("role"), event.get("request_id")) not in authoritative]
    return [(at, event) for event in decisions([*legacy, *rows])
            if (at := _time(event.get("at"))) is not None and at <= now]
