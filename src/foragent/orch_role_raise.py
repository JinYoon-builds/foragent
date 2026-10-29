"""Role signals raised to the orchestrator inbox by the gate tick (#794).

Code watches, the LLM judges. Every gate tick measures each role's signals with the
check `fa orch roles` uses and, for each met signal, appends one ordinary inbox request
(``from: gate``, ``role``, ``signal``). The orchestrator answers each one with
`fa orch signal-answer`. A signal never reaches the person.

Writers: the tick is the only writer of raised requests and of ``role-signal-clear``
events; `fa orch signal-answer` is the only writer of ``role-signal-answer`` events.
Raise times come from the inbox records themselves; nothing else is stored.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from foragent import orch_events, orch_inbox, orch_roles
from foragent.config import load_config
from foragent.i18n import text
from foragent.orch_facts_resources import _jsonl, _time
from foragent.orch_tick_outcome import step
from foragent.roles import load_roles
from foragent.state import StateStore

DEFAULT_COOLDOWN_HOURS = 12
CLEAR = "role-signal-clear"
ANSWER = "role-signal-answer"
ANSWERS = ("called", "not-now")


def cooldown(repo_root: Path) -> timedelta:
    """``role_signal_cooldown_hours`` from the repository's .foragent.yml; 12 hours when unset or invalid."""
    try:
        value = (yaml.safe_load((Path(repo_root) / ".foragent.yml").read_text(encoding="utf-8")) or {}).get(
            "role_signal_cooldown_hours", DEFAULT_COOLDOWN_HOURS)
    except (OSError, yaml.YAMLError, AttributeError):
        value = DEFAULT_COOLDOWN_HOURS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        value = DEFAULT_COOLDOWN_HOURS
    return timedelta(hours=value)


def source(signal: dict[str, Any]) -> dict[str, list[str]]:
    """A signal as the roles source names it: its paths and kinds items."""
    return {"paths": list(signal.get("paths") or []), "kinds": list(signal.get("kinds") or [])}


def _key(role: str, signal: dict[str, Any]) -> str:
    return json.dumps([role, source(signal)], sort_keys=True)


def is_signal_request(record: dict[str, Any]) -> bool:
    return record.get("from") == "gate" and bool(record.get("role")) and isinstance(record.get("signal"), dict)


def signal_requests(store: StateStore) -> list[dict[str, Any]]:
    """Every raised role-signal request, oldest first."""
    return [row for row in orch_inbox.requests(store) if is_signal_request(row)]


def answers(store: StateStore) -> dict[str, dict[str, Any]]:
    """The latest ``role-signal-answer`` event for each request id."""
    rows: dict[str, dict[str, Any]] = {}
    for event in _jsonl(store.root / "orch-events.jsonl"):
        if event.get("kind") == ANSWER and event.get("request_id"):
            rows[event["request_id"]] = event
    return rows


def _clears(store: StateStore) -> dict[str, list[datetime]]:
    rows: dict[str, list[datetime]] = {}
    for event in _jsonl(store.root / "orch-events.jsonl"):
        at = _time(event.get("at"))
        if event.get("kind") == CLEAR and at is not None and event.get("role") and isinstance(event.get("signal"), dict):
            rows.setdefault(_key(event["role"], event["signal"]), []).append(at)
    return rows


def _text(name: str, signal: dict[str, Any], files: set[str], kinds: set[str], numbers: dict[str, Any]) -> str:
    """The role, the signal and each matched part's numbers: matching files, or the kind's facts numbers."""
    parts = []
    if signal.get("paths") and (matched := orch_roles.matching_files(signal, files)):
        parts.append(f"{len(matched)} changed file(s) in the last 24 h match {', '.join(signal['paths'])}")
    parts += [f"fact {kind}: {json.dumps(numbers[kind], sort_keys=True)}" if numbers.get(kind) else f"fact {kind}"
              for kind in signal.get("kinds") or [] if kind in kinds]
    return (f"Role signal for {name}: {json.dumps(source(signal))} is met ({'; '.join(parts)}). "
            f"Call the role (fa orch {name} or its call command) and answer `fa orch signal-answer <id> called "
            f"--why ...`, or answer `not-now --why <reason>`.")


def raise_due(store: StateStore, repo_root: Path, now: datetime) -> list[dict[str, Any]]:
    """Raise every met role signal that is due; return the inbox requests added.

    A signal (role plus its {paths, kinds} item) is never raised while an earlier request
    for it is not done. After an answer, it is raised again when a tick saw it unmet since
    that raise, or when the cooldown passed since that raise.
    """
    if not (Path(repo_root) / ".foragent.yml").is_file():
        return []  # Not a Foragent repository: no roles source, nothing to measure.
    config = load_config(Path(repo_root))
    roles = load_roles(config)
    files, kinds, numbers = orch_roles.observe(store, config, Path(repo_root), roles)
    last: dict[str, dict[str, Any]] = {}
    for row in signal_requests(store):
        last[_key(row["role"], row["signal"])] = row
    clears, wait, added = _clears(store), cooldown(repo_root), []
    for name, entry in roles.items():
        for signal in entry["signals"]:
            key, previous = _key(name, signal), last.get(_key(name, signal))
            raised_at = _time(previous.get("created_at")) if previous else None
            cleared = raised_at is not None and any(at > raised_at for at in clears.get(key, []))
            if not orch_roles.signal_met(signal, files, kinds):
                if previous is not None and not cleared:
                    orch_events.append_event(store, now, None, CLEAR, f"{name}: signal cleared",
                                             role=name, signal=source(signal))
                    clears.setdefault(key, []).append(now)  # A repeated identical signal clears once.
                continue
            if previous is not None and (previous.get("status") != "done"
                                         or not (cleared or raised_at is None or now - raised_at >= wait)):
                continue
            last[key] = orch_inbox.add_request(store, "gate", _text(name, signal, files, kinds, numbers),
                                               {"role": name, "signal": source(signal)})
            added.append(last[key])  # A repeated identical signal sees this open request.
    return added


def tick(store: StateStore, repo_root: Path, now: datetime, language: str) -> list[dict[str, Any]]:
    """The gate tick's role-signal step; a failure is logged and never fails the tick."""
    with step("role_signals"):
        try:
            return raise_due(store, repo_root, now)
        except Exception as exc:  # noqa: BLE001 - one failed check must not stop the tick.
            print(text("tick_step_failed", language, step="role_signals", error=f"{type(exc).__name__}: {exc}"),
                  file=sys.stderr, flush=True)
            return []


def answer(store: StateStore, request_id: str, how: str, why: str) -> dict[str, Any]:
    """Record the orchestrator's answer to one role-signal request and mark it done."""
    if how not in ANSWERS:
        raise orch_inbox.InboxError(f"answer must be one of: {', '.join(ANSWERS)}")
    record = next((row for row in orch_inbox.requests(store) if row.get("id") == request_id), None)
    if record is None:
        raise orch_inbox.InboxError(f"no request with id {request_id!r}")
    if not is_signal_request(record):
        raise orch_inbox.InboxError(f"request {request_id} is not a role signal")
    if record.get("status") == "done":
        raise orch_inbox.InboxError(f"request {request_id} is already done")
    why = (why or "").strip()
    if how == "not-now" and not why:
        raise orch_inbox.InboxError("not-now needs a reason in --why")
    event = orch_events.append_event(store, store._now(), None, ANSWER, f"{request_id} {record['role']} {how}: {why}",
                                     request_id=request_id, role=record["role"], answer=how, why=why,
                                     signal=record["signal"])
    orch_inbox.set_request_status(store, request_id, "done", f"role signal {how}: {why}" if why else f"role signal {how}")
    return event


def add_parser(orch_actions) -> None:
    parser = orch_actions.add_parser("signal-answer", help="Answer one role-signal request: called or not-now")
    parser.add_argument("id", help="Request id of the role signal")
    parser.add_argument("answer", choices=ANSWERS)
    parser.add_argument("--why", default="", help="Why; required for not-now")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())


def command(args, store: StateStore) -> None:
    try:
        event = answer(store, args.id, args.answer, args.why)
    except orch_inbox.InboxError as exc:
        raise SystemExit(f"fa orch signal-answer: {exc}") from None
    print(f"Role {event['role']}: signal {args.id} answered {args.answer}.")
