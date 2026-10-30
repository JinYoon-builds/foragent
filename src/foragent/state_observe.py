"""The one writer of observed GitHub issue facts (#1025).

Every observation of an issue (a bulk listing or a single read) lands through
``apply_observation`` under the store lock. It
compares the observation time with the stored one first: an older observation
applies nothing at all, a newer one replaces every observed field together in the
row's ``github`` record. Local writes are an overlay on that record: queued writes
until an observation taken at or after their queue time confirms or contradicts
them, and writes GitHub accepted (``record_result``) until an observation taken at
or after their acceptance.
"""

from datetime import UTC, datetime
from typing import Any

from foragent.labels import issue_lane

# The issue facts a GitHub observation carries; observed_at is when it began.
OBSERVED = ("title", "body", "labels", "lane", "state", "state_reason", "observed_at")


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def observed_record(row: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """The last observed facts.

    A row from before #1025 has no ``github`` record. Its own fields then hold queued
    intent too, so an issue with a delivery baseline reads that instead; otherwise the
    row's fields are its last observation."""
    if row.get("github"):
        return row["github"]
    base = ((snapshot or {}).get("outbox_base") or {}).get(str(row.get("number")))
    if base is not None and any(entry["issue"] == row.get("number") for entry in (snapshot or {}).get("outbox", [])):
        return {key: base.get(key) for key in OBSERVED}
    return {key: row.get(key) for key in OBSERVED}


def record_removal(snapshot: dict[str, Any], number: int, at: datetime) -> None:
    """Keep when an issue was seen gone, so an older observation cannot bring it back."""
    removed = snapshot.setdefault("removed", {})
    if (_time(removed.get(str(number))) or at) <= at:
        removed[str(number)] = at.isoformat()


def newer_than(row: dict[str, Any] | None, at: datetime, snapshot: dict[str, Any] | None = None) -> bool:
    """Whether the row holds an observation taken after ``at``."""
    seen = _time(observed_record(row, snapshot).get("observed_at")) if row else None
    return seen is not None and seen > (at if at.tzinfo is not None else at.replace(tzinfo=UTC))


def overlay(facts: dict[str, Any], entries: list[dict[str, Any]], lanes: dict | None,
            lane: str | None = None) -> dict[str, Any]:
    """The facts with local write effects on top; comments are not observed facts."""
    out = dict(facts)
    for entry in entries:
        action = entry["action"]
        if action in {"add_labels", "remove_labels"}:
            current = set(out["labels"])
            labels = current - set(entry["labels"]) if action == "remove_labels" else current | set(entry["labels"])
            out["labels"] = sorted(labels)
        elif action == "update_issue":
            out.update(title=entry["title"], body=entry["body"])
        elif action == "close_issue":
            out["state"] = "closed"
        elif action == "facts":
            out.update(entry["fields"])
    if out.get("labels") != facts.get("labels"):
        out["lane"] = issue_lane(out["labels"], lanes) if lanes is not None else (lane or out.get("lane"))
    return out


def _shown(facts: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Whether the facts already show a write's whole effect. A comment, including a close
    comment, is not an observed fact, so it is never shown."""
    action = entry["action"]
    if action == "add_labels":
        return set(entry["labels"]) <= set(facts["labels"])
    if action == "remove_labels":
        return not set(entry["labels"]) & set(facts["labels"])
    if action == "update_issue":
        return (facts["title"], facts["body"]) == (entry["title"], entry["body"])
    if action == "close_issue":
        return facts["state"] == "closed" and entry.get("body") is None
    return False


def apply_observation(snapshot: dict[str, Any], row: dict[str, Any], facts: dict[str, Any], at: datetime,
                      *, lanes: dict | None = None, local_since: int | None = None,
                      forget_reads: bool = True) -> tuple[bool, bool]:
    """Apply one observation of the row's issue; return (applied, queued writes dropped).

    The caller holds the store lock. At an equal time a listing that began before a
    local change (``local_since``) is the older one."""
    number = row["number"]
    at = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    stored = _time(observed_record(row, snapshot).get("observed_at"))
    if stored is not None and (at < stored or at == stored and local_since is not None
                               and row.get("revision", 0) > local_since):
        return False, False
    removed = _time((snapshot.get("removed") or {}).get(str(number)))
    if removed is not None and at <= removed:
        # A listing at ``removed`` showed the issue gone; an observation not after it is older.
        return False, False
    (snapshot.get("removed") or {}).pop(str(number), None)
    record = {**{key: facts[key] for key in OBSERVED if key != "observed_at"}, "observed_at": at.isoformat()}
    outbox = snapshot.get("outbox", [])
    pending = [entry for entry in outbox if entry["issue"] == number]
    judged = [entry for entry in pending if (_time(entry.get("queued_at")) or at) <= at]
    bases = snapshot.get("outbox_base", {})
    base = bases.get(str(number))
    dropped = False
    if judged and base is not None:
        # The observation began after these writes were queued, so someone changing the
        # issue contradicts them. A reopening or a new close reason changes state_reason
        # even when state matches; a baseline from before #1025 may lack it.
        fields = {"labels", "state"} | ({"state_reason"} & base.keys()) | ({"title", "body"} if any(
            entry["action"] == "update_issue" for entry in judged) else set())
        if any(record[key] != base.get(key) for key in fields):
            ids = {entry["id"] for entry in judged}
            snapshot["outbox"] = [entry for entry in outbox if entry["id"] not in ids]
            pending = [entry for entry in pending if entry["id"] not in ids]
            dropped = True
    unconfirmed = [entry for entry in row.get("unconfirmed", [])
                   if (_time(entry.get("delivered_at")) or at) > at]
    if judged and not dropped:
        # A judged write is confirmed when GitHub, with the earlier writes still queued on
        # top, already shows its whole effect: delivering it would change nothing.
        facts_now, kept, retired = overlay(record, unconfirmed, lanes), [], set()
        judged_ids = {entry["id"] for entry in judged}
        for entry in pending:
            if entry["id"] in judged_ids and _shown(facts_now, entry):
                retired.add(entry["id"])
            else:
                kept.append(entry)
                facts_now = overlay(facts_now, [entry], lanes)
        if retired:
            snapshot["outbox"] = [entry for entry in snapshot["outbox"] if entry["id"] not in retired]
            pending = kept
    if pending and base is not None:
        base.update(overlay(record, unconfirmed, lanes, base.get("lane")))
    elif not pending:
        bases.pop(str(number), None)
    current = overlay(record, [*unconfirmed, *pending], lanes, row.get("lane"))
    # A bulk listing confirms remembered reads by its own facts (confirmed_read); other
    # observations of a change make them unconfirmed.
    if forget_reads and any(row.get(key) != current[key] for key in OBSERVED if key != "observed_at"):
        _state._forget_reads(row)
    row.update(current, github=record)
    if unconfirmed:
        row["unconfirmed"] = unconfirmed
    else:
        row.pop("unconfirmed", None)
    return True, dropped


def record_result(snapshot: dict[str, Any], row: dict[str, Any], entry: dict[str, Any], at: str,
                  lanes: dict | None = None) -> None:
    """A write GitHub accepted: an overlay on the observed record, never a second writer of it,
    until an observation taken at or after ``at`` shows it."""
    row.setdefault("unconfirmed", []).append({**entry, "delivered_at": at})
    pending = [item for item in snapshot.get("outbox", []) if item["issue"] == row["number"]]
    row.update(overlay(observed_record(row, snapshot), [*row["unconfirmed"], *pending], lanes, row.get("lane")))


def confirmed(row: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """The row as GitHub last showed it, plus writes GitHub accepted since; no queued intent."""
    facts = overlay(observed_record(row, snapshot), row.get("unconfirmed", []), None, row.get("lane"))
    return {**row, **facts}


# Bound last: foragent.state imports the state modules, and lookups happen at call time.
import foragent.state as _state
