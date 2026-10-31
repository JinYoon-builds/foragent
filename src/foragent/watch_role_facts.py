"""Architect and docs session facts for the lane view: liveness, caller short, last call.

Reads each role's own record (orch_architect.read_record); the full why is never used.
"""

from typing import Any

from foragent.state import StateStore

ROLES = ("architect", "docs")


def collect(store: StateStore) -> dict[str, dict[str, Any]]:
    """Both roles, in order; a missing or unreadable record reads as never called."""
    from foragent import orch_architect

    facts: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        try:
            record = orch_architect.read_record(store, role=role)
        except (OSError, ValueError):
            record = None
        short = record.get("short") if record else None
        started = record.get("started_at") if record else None
        facts[role] = {
            "alive": bool(record) and bool(orch_architect.session_alive(record)),
            "short": short if isinstance(short, str) and short.strip() else None,
            "last_called_at": started if isinstance(started, str) and started else None,
        }
    return facts
