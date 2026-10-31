"""Observed issue facts and the local write intent log, each with one writer.

``issues`` keeps, per issue, the latest observation and the time its read
started (``observed_at``); ``absent`` keeps missing-issue observations and
``complete_at`` the start of the latest complete listing. Only ingest writes
them. ``outbox`` is the append-only intent log: each intent records when it
was queued, the facts it was queued against (``premise``) and its send
``attempts``. Only ingest removes intents, by one rule (``retirement``). The
visible issue and the pending outbox are derived from the two, never stored.
"""

from datetime import UTC, datetime
from typing import Any

FACTS = ("title", "body", "labels", "state", "state_reason")
EARLIEST = datetime.min.replace(tzinfo=UTC)


def stamp(value: Any) -> datetime | None:
    try:
        seen = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return seen if seen.tzinfo is not None else seen.replace(tzinfo=UTC)


def ok_starts(intent: dict[str, Any]) -> list[datetime]:
    return [stamp(attempt["started_at"]) for attempt in intent.get("attempts", []) if attempt.get("ok")]


def sent_labels(intent: dict[str, Any], by: datetime | None = None) -> set[str]:
    """Labels this intent's ok sends delivered; with ``by``, only sends started by then."""
    sent: set[str] = set()
    for attempt in intent.get("attempts", []):
        if attempt.get("ok") and (by is None or (stamp(attempt["started_at"]) or EARLIEST) <= by):
            sent |= set(attempt.get("labels", intent.get("labels", [])))
    return sent


def sent_at(intent: dict[str, Any]) -> datetime | None:
    """When the send that completed the intent started; None while any part is unsent."""
    starts = ok_starts(intent)
    if not starts or intent["action"] == "remove_labels" and not set(intent["labels"]) <= sent_labels(intent):
        return None
    return max(starts)


def comment_id(intent: dict[str, Any]) -> int:
    ids = [attempt["comment_id"] for attempt in intent.get("attempts", [])
           if attempt.get("ok") and attempt.get("comment_id") is not None]
    return ids[-1] if ids else -intent["id"]


def queue_order(intent: dict[str, Any]) -> tuple[datetime, int]:
    return stamp(intent["queued_at"]) or EARLIEST, intent["id"]


def effect(facts: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    """The issue facts after the intent's effect (comments are not issue facts)."""
    action, result = intent["action"], dict(facts)
    if action == "add_labels":
        result["labels"] = sorted(set(facts["labels"]) | set(intent["labels"]))
    elif action == "remove_labels":
        result["labels"] = sorted(set(facts["labels"]) - set(intent["labels"]))
    elif action == "update_issue":
        result.update(title=intent["title"], body=intent["body"])
    elif action == "close_issue":
        result["state"] = "closed"
    return result


def shows(facts: dict[str, Any], intent: dict[str, Any]) -> bool:
    """The facts already show the intent's effect; a no-op counts. A comment never shows."""
    if intent["action"] == "comment" or intent["action"] == "close_issue" and intent.get("body") is not None:
        return False
    return effect(facts, intent) == facts


def person_fields(facts: dict[str, Any]) -> tuple[list[str], str]:
    """The fields a person's change is detected in: labels and open/closed state."""
    return sorted(facts.get("labels", [])), facts.get("state", "open")


def retirement(facts: dict[str, Any] | None, at: datetime, intent: dict[str, Any],
               expected: dict[str, Any] | None, *, by_person: bool = True) -> str | None:
    """The one retirement rule: why an observation retires an intent, or None.

    ``facts`` is the observation (None for absence) whose read started at
    ``at``; ``expected`` is it with the earlier still-pending intents
    applied, which is what shows (or not) this intent's effect and what its
    premise is compared with. A local update by a command (``by_person``
    False) is our own change, so it never drops an intent by its premise.
    """
    if at < (stamp(intent["queued_at"]) or EARLIEST):
        return None
    if facts is None:
        return "absent"
    if shows(expected, intent):
        return "shown"
    sent = sent_at(intent)
    if sent is not None:
        return "sent" if at >= sent else None
    premise = intent.get("premise")
    if premise is not None and intent["action"] == "remove_labels":
        # Labels this intent's own sends removed before the read started are not a person's
        # change; a send that started after the read cannot be in what it shows.
        premise = {**premise, "labels": sorted(set(premise["labels"]) - sent_labels(intent, by=at))}
    if by_person and premise is not None and person_fields(premise) != person_fields(expected):
        return "dropped"
    return None


def observed_facts(row: dict[str, Any]) -> dict[str, Any]:
    return {"title": row.get("title", ""), "body": row.get("body", ""), "labels": sorted(row.get("labels", [])),
            "state": row.get("state", "open"), "state_reason": row.get("state_reason")}


def issue_facts(issue: Any) -> dict[str, Any]:
    return {"title": issue.title, "body": issue.body, "labels": sorted(issue.labels), "state": issue.state,
            "state_reason": getattr(issue, "state_reason", None)}


def missing_since(snapshot: dict[str, Any], number: int) -> datetime | None:
    """When the issue was last observed missing: its absence record, or the latest
    complete listing if no read since observed it."""
    key = str(number)
    stored = stamp((snapshot["issues"].get(key) or {}).get("observed_at"))
    missing = stamp(snapshot.get("absent", {}).get(key))
    listed = stamp(snapshot.get("complete_at"))
    if listed is not None and (stored is None or stored < listed):
        missing = max(missing or listed, listed)
    return missing


def latest(snapshot: dict[str, Any], number: int) -> tuple[dict[str, Any] | None, datetime | None]:
    """The issue's latest observation: its facts (None when it was missing) and when its read started."""
    row = snapshot["issues"].get(str(number))
    stored = stamp((row or {}).get("observed_at"))
    missing = missing_since(snapshot, number)
    if missing is not None and (stored is None or missing > stored):
        return None, missing
    return (observed_facts(row), stored) if stored is not None else (None, None)


def tombstoned(snapshot: dict[str, Any], number: int, at: datetime) -> bool:
    """A local change at ``at`` has no issue to change: it was found missing (any time), or a
    complete listing that started after the change omitted it."""
    facts, seen = latest(snapshot, number)
    return seen is not None and facts is None and (
        str(number) in snapshot.get("absent", {}) or seen > at)


class IssueObservationsMixin:
    def _retire(self, snapshot: dict[str, Any], number: int, facts: dict[str, Any] | None,
                at: datetime, *, by_person: bool = True) -> bool:
        """Apply the retirement rule to the issue's intents in queue order.

        Retired intents leave the log; a sent comment or ready-label change is
        kept on the row. Returns whether an intent was dropped unsent.
        """
        intents = sorted((intent for intent in snapshot.get("outbox", []) if intent["issue"] == number),
                         key=queue_order)
        legacy = snapshot.get("outbox_base", {}).get(str(number))
        expected = dict(facts) if facts is not None else None
        gone, dropped = set(), False
        row = snapshot["issues"].get(str(number))
        for intent in intents:
            if intent.get("premise") is None and legacy is not None:
                intent = {**intent, "premise": observed_facts(legacy)}
            reason = retirement(facts, at, intent, expected, by_person=by_person)
            if reason is None:
                expected = effect(expected, intent) if expected is not None else None
                continue
            gone.add(intent["id"])
            if reason in ("absent", "dropped") and sent_at(intent) is None:
                dropped = True
            elif row is not None:
                self._keep_local_record(row, intent)
        snapshot["outbox"] = [intent for intent in snapshot.get("outbox", []) if intent["id"] not in gone]
        if not any(intent["issue"] == number for intent in snapshot["outbox"]):
            snapshot.get("outbox_base", {}).pop(str(number), None)
        return dropped

    def _keep_local_record(self, row: dict[str, Any], intent: dict[str, Any]) -> None:
        """A retired intent leaves what the issue facts do not show: its comment and ready approval."""
        from foragent.labels import READY

        if intent["action"] == "comment" or intent["action"] == "close_issue" and intent.get("body") is not None:
            identity = comment_id(intent)
            if all(comment["id"] != identity for comment in row.get("comments", [])):
                row["comments"] = row.get("comments", []) + [{
                    "id": identity, "body": intent["body"], "author": "foragent", "created_at": intent["queued_at"]}]
        if intent["action"] in {"add_labels", "remove_labels"} and READY in intent["labels"]:
            approval = self.load_approval(row["number"])
            row["ready_approval"] = (None if intent["action"] == "remove_labels" or approval is None
                                     else approval.approved_at)

    def _observe(self, snapshot: dict[str, Any], number: int, facts: dict[str, Any] | None, at: datetime,
                 lanes: dict | None, *, since_revision: int | None = None, by_person: bool = True) -> str:
        """Ingest one observation; the single writer of observed facts.

        Returns "stale" when the stored observation started later (nothing
        changes), "dropped" when it dropped an intent unsent, else "applied".
        Equal start times let this later ingest win, except in a bulk refresh
        (``since_revision``) against a row saved after the refresh began.
        """
        from foragent.labels import issue_lane

        key = str(number)
        row = snapshot["issues"].get(key)
        stored = stamp((row or {}).get("observed_at"))
        missing = missing_since(snapshot, number)
        if stored is not None and (stored > at or stored == at and since_revision is not None
                                   and (row or {}).get("revision", 0) > since_revision):
            return "stale"
        if facts is not None and missing is not None and missing > at:
            return "stale"
        if facts is None:
            absent = snapshot.setdefault("absent", {})
            if missing is None or missing <= at:
                absent[key] = at.isoformat()
            dropped = self._retire(snapshot, number, None, at)
            if not any(intent["issue"] == number for intent in snapshot.get("outbox", [])):
                snapshot["issues"].pop(key, None)
            return "dropped" if dropped else "applied"
        snapshot.setdefault("absent", {}).pop(key, None)
        row = snapshot["issues"].setdefault(key, self._new_row(number))
        if observed_facts(row) != facts:
            _state._forget_reads(row)
        lane = issue_lane(facts["labels"], lanes) if lanes is not None else row.get("lane", "main")
        row.update(facts, lane=lane, observed_at=at.isoformat())
        return "dropped" if self._retire(snapshot, number, facts, at, by_person=by_person) else "applied"

    def _new_row(self, number: int) -> dict[str, Any]:
        return {"number": number, "title": "", "body": "", "labels": [], "lane": "main",
                "state": "open", "pr_number": None, "merged": False, "comments": []}

    def ingest(self, issues: list, started_at: datetime, lanes: dict, *, complete: bool = False) -> list[int]:
        """Ingest what one read that started at ``started_at`` showed.

        A complete listing (every issue, any state) also observes each issue
        it omits as absent. Returns the issues whose intents were dropped unsent.
        """
        with self._state_lock():
            snapshot = self._issue_snapshot()
            dropped, seen, changed = set(), set(), False
            for issue in issues:
                if issue.is_pull_request:
                    continue
                seen.add(issue.number)
                outcome = self._observe(snapshot, issue.number, issue_facts(issue), started_at, lanes)
                changed |= outcome != "stale"
                if outcome == "dropped":
                    dropped.add(issue.number)
            if complete:
                if (stamp(snapshot.get("complete_at")) or EARLIEST) <= started_at:
                    snapshot["complete_at"] = started_at.isoformat()
                    changed = True
                for key in list(snapshot["issues"]):
                    if int(key) not in seen and self._observe(snapshot, int(key), None, started_at, lanes) == "dropped":
                        dropped.add(int(key))
            if changed:
                self._save_rows(snapshot, seen)
            return sorted(dropped)

    def ingest_absence(self, number: int, started_at: datetime) -> bool:
        """A read that started at ``started_at`` found the issue missing; returns whether intents were dropped."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            outcome = self._observe(snapshot, number, None, started_at, {})
            if outcome != "stale":
                self._save_rows(snapshot, ())
            return outcome == "dropped"

    def _save_rows(self, snapshot: dict[str, Any], numbers: Any) -> None:
        snapshot["revision"] = snapshot.get("revision", 0) + 1
        for number in numbers:
            if (row := snapshot["issues"].get(str(number))) is not None:
                row.update(revision=snapshot["revision"], updated_at=self._timestamp())
        self._write_json(self.root / "issues.json", snapshot)

    def record_github_attempt(self, intent_id: int, started_at: datetime, ok: bool, *,
                              labels: list[str] | None = None, comment: int | None = None) -> None:
        """Append one send attempt to its intent; an intent already retired gains nothing."""
        with self._state_lock():
            snapshot = self._issue_snapshot()
            intent = next((item for item in snapshot.get("outbox", []) if item["id"] == intent_id), None)
            if intent is None:
                return
            attempt: dict[str, Any] = {"started_at": started_at.astimezone(UTC).isoformat(), "ok": ok}
            if labels is not None:
                attempt["labels"] = list(labels)
            if comment is not None:
                attempt["comment_id"] = comment
            intent.setdefault("attempts", []).append(attempt)
            facts, seen = latest(snapshot, intent["issue"])
            if ok and seen is not None and seen > started_at:
                # A read that started during this send was stored before the send completed:
                # it judges the now sent intent by the same rule as a later read.
                self._retire(snapshot, intent["issue"], facts, seen)
            self._save_rows(snapshot, (intent["issue"],))

    def _visible(self, snapshot: dict[str, Any], row: dict[str, Any], lanes: dict | None = None) -> dict[str, Any]:
        """The latest observation plus the issue's unretired intents in queue order."""
        visible = _copy(row)
        for intent in sorted((item for item in snapshot.get("outbox", []) if item["issue"] == row["number"]),
                             key=queue_order):
            known = {name: {} for name in intent.get("lanes", [])}
            item = {**intent, "id": -comment_id(intent)}
            if any(comment["id"] == -item["id"] for comment in visible.get("comments", [])):
                # A comment read already shows this sent comment.
                if intent["action"] == "comment":
                    continue
                item.pop("body", None)
            self._apply_github_write(visible, item, lanes or known)
        return visible

    def _pending(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Unretired intents without a successful send, in queue order, with the labels still to send."""
        pending = []
        for intent in sorted(snapshot.get("outbox", []), key=queue_order):
            if sent_at(intent) is not None:
                continue
            entry = {key: _copy(value) for key, value in intent.items() if key not in ("premise", "attempts", "lanes")}
            if intent["action"] == "remove_labels":
                entry["labels"] = [label for label in intent["labels"] if label not in sent_labels(intent)]
            pending.append(entry)
        return pending


def _copy(value: Any) -> Any:
    return _state_issues._copy(value)


# Bound last: foragent.state imports this module, and lookups happen at call time
# so names patched on the facade reach this implementation.
import foragent.state as _state
import foragent.state_issues as _state_issues
