"""When tidying is due, from local facts only (D68, D82).

The schedule is a fact check, not a decision: 30 merges since the last tidy, or
every lane idle while recorded plan quota is left. When it is due, the gate
puts one 'tidy due' request with the numbers into the orchestrator's inbox per
due period. The orchestrator judges what to do with it; nothing here orders or
runs tidy work. ``fa tidy done`` starts the next period.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from foragent import orch_inbox
from foragent.state import StateStore

MERGE_THRESHOLD = 30
_START = "start"  # Period key before the first recorded tidy.


def _merged_runs(store: StateStore) -> int:
    return sum(run.outcome == "merged" for run in store.read_history())


def _quota_left(store: StateStore, now: datetime) -> list[str]:
    """One evidence line per executor whose recorded windows all have quota left."""
    lines = []
    for executor, record in sorted(store.usage().items()):
        windows = record.get("windows") or []
        if not windows:
            continue
        parts, left = [], True
        for window in windows:
            used = float(window.get("used_percent", 0))
            reset = window.get("resets_at")
            if used >= 100 and not (reset and datetime.fromisoformat(reset) <= now):
                left = False
            parts.append(f"{window.get('name')} {used:g}% used ({max(0.0, 100 - used):g}% remaining"
                         + (f", resets {reset}" if reset else "") + ")")
        if left:
            lines.append(f"{executor}: " + "; ".join(parts) + f" (seen {record.get('seen_at')})")
    return lines


def status(store: StateStore) -> dict[str, Any]:
    """The ``fa tidy due --json`` facts plus the evidence lines behind them."""
    state = store.tidy_state()
    merges = _merged_runs(store) - int(state.get("merged_at_tidy", 0))
    reason, evidence = None, []
    active = store.active_runs()
    if merges >= MERGE_THRESHOLD:
        reason = "merges"
    elif merges > 0 and not active and (quota := _quota_left(store, store._now())):
        reason, evidence = "idle-quota", ["no lane is running", *quota]
    return {
        "due": reason is not None, "reason": reason, "merges_since": merges,
        "last_tidy_at": state.get("last_tidy_at"), "evidence": evidence,
        "active_runs": len(active),
    }


def public(facts: dict[str, Any]) -> dict[str, Any]:
    return {key: facts[key] for key in ("due", "reason", "merges_since", "last_tidy_at")}


def done(store: StateStore) -> dict[str, Any]:
    """Record a finished tidy; the merge count and the due period start again."""
    merged = _merged_runs(store)

    def change(state: dict[str, Any]) -> bool:
        state.update(last_tidy_at=store._timestamp(), merged_at_tidy=merged)
        return True

    store.update_tidy_state(change)
    return public(status(store))


def request_text(facts: dict[str, Any]) -> str:
    last = facts["last_tidy_at"] or "never"
    lines = [
        f"Tidy due ({facts['reason']}): merges since the last tidy: {facts['merges_since']} "
        f"(threshold {MERGE_THRESHOLD}); last tidy: {last}.",
        *facts["evidence"],
        "Judge whether to call the architect (`fa orch architect --why ...`) and propose a tidy order "
        "for approval; run `fa tidy done` after the tidy work merges.",
    ]
    return "\n".join(lines)


def post_if_due(store: StateStore) -> dict[str, Any] | None:
    """Put one 'tidy due' request in the inbox per due period; return it when posted.

    The request itself records its period, so the inbox is the only proof that a
    period was posted: a failed or interrupted write leaves nothing that suppresses
    the next tick's retry. Callers run under the tick lock, one at a time.
    """
    facts = status(store)
    if not facts["due"]:
        return None
    period = facts["last_tidy_at"] or _START
    if any(row["from"] == "gate" and row.get("tidy_period") == period
           for row in orch_inbox.requests(store)):
        return None
    return orch_inbox.add_request(store, "gate", request_text(facts), {"tidy_period": period})
