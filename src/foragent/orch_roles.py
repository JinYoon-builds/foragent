"""`fa orch roles` and `fa orch role-decision`: each role's call signals, calls and proposal record (#772).

Everything is read from local records: the ``roles`` source in .foragent.yml, the run
history, the orchestrator journal and the kinds that ``fa orch facts`` reports. Listing
roles writes nothing and calls nothing; calling a role stays the orchestrator's judgment.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from foragent import orch_events, orch_facts, orch_inbox
from foragent.config import Config
from foragent.orch_facts_resources import _jsonl, _time
from foragent.policy import _matches
from foragent.roles import DEFAULT_ROLES, load_roles
from foragent.state import StateStore

PATH_WINDOW = timedelta(hours=24)
# A custom role whose last this-many decided proposals were all rejected is proposed for removal.
REJECTED_IN_A_ROW = 3


def call_kind(name: str) -> str:
    """The journal kind of one role call; old ``architect-call`` events count for the architect."""
    return f"{name}-call"


def _events(store: StateStore, now: datetime) -> list[tuple[datetime, dict[str, Any]]]:
    """Journal events at or before now, oldest first."""
    rows = [(at, event) for event in _jsonl(store.root / "orch-events.jsonl")
            if (at := _time(event.get("at"))) is not None and at <= now]
    return sorted(rows, key=lambda row: row[0])


def _recent_merged_files(store: StateStore, now: datetime) -> set[str]:
    """Files changed by merged executions that finished in the last 24 hours."""
    finished = store.finish_times()
    files: set[str] = set()
    for run in store.read_history():
        at = _time(finished.get((run.number, run.started_at)))
        if run.outcome == "merged" and at is not None and now - PATH_WINDOW < at <= now:
            files.update(run.changed_files or [])
    return files


def _met(signal: dict[str, Any], files: set[str], kinds: set[str]) -> bool:
    return (any(_matches(path, glob) for path in files for glob in signal.get("paths") or [])
            or any(kind in kinds for kind in signal.get("kinds") or []))


def _removal_proposed(name: str, entry: dict[str, Any], events, now: datetime) -> bool:
    if name in DEFAULT_ROLES:
        return False
    calls = [at for at, event in events if event.get("kind") == call_kind(name)]
    decisions = [event.get("accepted") for _, event in events
                 if event.get("kind") == "role-proposal" and event.get("role") == name
                 and type(event.get("accepted")) is bool]
    if len(decisions) >= REJECTED_IN_A_ROW and not any(decisions[-REJECTED_IN_A_ROW:]):
        return True
    # Idle since the last call; with no call, since creation; with neither, from now.
    since = calls[-1] if calls else (_time(entry.get("created_at")) or now)
    return now - since > timedelta(days=entry.get("idle_days") or 0)


def collect(store: StateStore, config: Config, root: Path) -> dict[str, Any]:
    roles = load_roles(config)
    now = store._now()
    events = _events(store, now)
    files = _recent_merged_files(store, now)
    wanted = any(signal.get("kinds") for entry in roles.values() for signal in entry["signals"])
    # The same snapshot `fa orch facts` shows, read only when some signal names a kind.
    kinds = set(orch_facts.collect(store, config, root, checkout=True).get("kinds") or []) if wanted else set()
    rows = {}
    for name, entry in roles.items():
        signals = [{"paths": list(signal.get("paths") or []), "kinds": list(signal.get("kinds") or []),
                    "met": _met(signal, files, kinds)} for signal in entry["signals"]]
        last = max((at for at, event in events if event.get("kind") == call_kind(name)), default=None)
        rows[name] = {"default": name in DEFAULT_ROLES, "signals": signals,
                      "last_call": last and last.isoformat(),
                      "removal_proposed": _removal_proposed(name, entry, events, now)}
    return {"at": now.isoformat(), "roles": rows}


def counts(names: list[str], events: list[tuple[int, dict[str, Any]]], days: int) -> list[dict]:
    """Per day bucket, each role's calls and accepted proposals from (bucket, event) pairs."""
    daily = [{name: {"calls": 0, "accepted_proposals": 0} for name in names} for _ in range(days)]
    for bucket, event in events:
        kind, role = event.get("kind"), event.get("role")
        for name in names:
            if kind == call_kind(name):
                daily[bucket][name]["calls"] += 1
            elif kind == "role-proposal" and role == name and event.get("accepted") is True:
                daily[bucket][name]["accepted_proposals"] += 1
    return daily


def human(payload: dict[str, Any]) -> str:
    lines = []
    for name, row in payload["roles"].items():
        met = [", ".join(signal["paths"] + signal["kinds"]) for signal in row["signals"] if signal["met"]]
        lines.append(f"{name}: " + (f"signals met: {'; '.join(met)}" if met else
                                     f"{len(row['signals'])} signal(s), none met")
                     + f"; last call {row['last_call'] or 'never'}"
                     + ("; removal proposed" if row["removal_proposed"] else ""))
    return "\n".join(lines) or "No roles."


def decide(store: StateStore, request_id: str, decision: str, role: str | None) -> dict[str, Any]:
    """Record the orchestrator's decision on one role proposal and close its request."""
    record = next((row for row in orch_inbox.requests(store) if row.get("id") == request_id), None)
    if record is None:
        raise orch_inbox.InboxError(f"no request with id {request_id!r}")
    name = record.get("role") or role
    if not name:
        raise orch_inbox.InboxError(f"request {request_id} names no role; pass --role")
    event = orch_events.append_event(store, store._now(), None, "role-proposal",
                                     f"{name}: {request_id} {decision}", role=name, request_id=request_id,
                                     accepted=decision == "accepted")
    orch_inbox.set_request_status(store, request_id, "done", f"role {name} proposal {decision}")
    return event


def add_parser(orch_actions) -> None:
    roles = orch_actions.add_parser("roles", help="List every role with its signals and whether each is met")
    roles.add_argument("--repo-root", type=Path, default=Path.cwd())
    roles.add_argument("--json", action="store_true")
    decision = orch_actions.add_parser("role-decision", help="Record a role proposal as accepted or rejected")
    decision.add_argument("id", help="Request id of the proposal")
    decision.add_argument("decision", choices=("accepted", "rejected"))
    decision.add_argument("--role", help="Role name when the request names none")
    decision.add_argument("--repo-root", type=Path, default=Path.cwd())


def command(args, store: StateStore, config: Config) -> None:
    if args.action == "role-decision":
        event = decide(store, args.id, args.decision, args.role)
        print(f"Role {event['role']}: proposal {args.id} {args.decision}.")
        return
    payload = collect(store, config, args.repo_root.resolve())
    print(json.dumps(payload, ensure_ascii=False) if args.json else human(payload))
