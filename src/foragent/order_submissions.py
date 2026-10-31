"""Publication-owned retained orders; planning's journal provides lock-free appends."""

from datetime import UTC, datetime

from foragent import order_notes
from foragent.i18n import text
from foragent.order import PlanError
from foragent.state import StateStore


def retain(plan: dict, repo: str, boundary: str, *, approve: bool,
           approve_keys: list[str] | None, approve_numbers: list[int] | None,
           language: str) -> None:
    if not boundary.strip():
        raise PlanError(text("order_submission_boundary", language))
    # Selection is evidence supplied by the caller, never a derived "approved" fact.
    # A future publisher must validate the retained plan and preread again.
    row = {"id": plan["id"], "plan": plan, "approve": approve,
           "approve_keys": approve_keys or [], "approve_numbers": approve_numbers or [],
           "boundary": boundary, "recorded_at": datetime.now(UTC).isoformat(), "status": "waiting"}
    try:
        order_notes.append_entry(StateStore.for_repo(repo), row, journal="orders.jsonl", language=language)
    except OSError as exc:
        raise PlanError(text("order_submission_io", language, error=exc)) from exc


def pending(store) -> list[dict]:
    latest = {}
    for row in order_notes.entries(store, "orders.jsonl"):
        latest[row["id"]] = row
    return list(latest.values())
