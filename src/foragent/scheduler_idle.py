"""Wake the orchestrator when every lane is idle while issues wait for approval (#2842).

PM r1211 (11-08): lanes sat idle for an hour while ready issues waited for approval, until the
PM asked by hand. Once per idle stretch the gate tick puts one request in the orchestrator inbox
that names the waiting issues, so the orchestrator prereads them and approves the clean ones.

Nothing new is stored. The idle stretch is derived from the run records: any live run (an issue
run, a lane dream or a lane test draft; ``StateStore.active_runs``) means a lane works, and the
stretch began at the latest start or finish of any run, from the issue history (``runs.jsonl``)
and every record under ``runs/`` (side runs never enter the history and keep their finish time
only in their own file). A new run always moves that time, so each stretch has its own key.
The inbox request carries the key, so the request itself is the only receipt: any request with
the same key, in any status, means this stretch was already told.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from foragent import orch_facts_stall, orch_inbox
from foragent.state import StateStore

FIELD = "idle_stretch"


def _at(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def stretch(store: StateStore) -> str | None:
    """The current idle stretch's key, or None while any run is live."""
    if store.active_runs():
        return None
    stamps = [value for row in store._run_history() for value in (row.get("started_at"), row.get("finished_at"))]
    for path in (store.root / "runs").glob("*.json"):
        if not (path.stem.isdigit() or path.stem.startswith(("dream-", "draft-"))):
            continue  # Only run records (active_runs reads the same names), not other run files.
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict):
            stamps += [row.get("started_at"), row.get("finished_at")]
    times = [at for value in stamps if value and (at := _at(value)) is not None]
    return max(times).astimezone(UTC).isoformat() if times else "start"


def request_text(numbers: list[int]) -> str:
    listed = ", ".join(f"#{number}" for number in numbers)
    return (f"Every lane is idle while {len(numbers)} ready issue(s) wait for approval: {listed}. "
            "Preread them and approve the clean ones now.")


def tick(store: StateStore) -> dict[str, Any] | None:
    """Add the stretch's one request when every lane is idle and ready issues wait for approval.

    Runs under the tick lock, so the check and the write cannot race another tick."""
    key = stretch(store)
    if key is None:
        return None
    numbers = orch_facts_stall.unapproved_ready_numbers(store)
    if not numbers:
        return None
    if any(row.get("from") == "gate" and row.get(FIELD) == key for row in orch_inbox.requests(store)):
        return None
    return orch_inbox.add_request(store, "gate", request_text(numbers), {FIELD: key, "issues": numbers})
