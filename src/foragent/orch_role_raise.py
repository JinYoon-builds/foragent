"""Role signals raised to the orchestrator inbox by the gate tick (#794).

Code watches, the LLM judges. Every gate tick measures each role's signals with the
check `fa orch roles` uses and, for each met signal, appends one ordinary inbox request
(``from: gate``, ``role``, ``signal``). The orchestrator answers each one with
`fa orch signal-answer`. A signal never reaches the person.

Writers: the tick is the only writer of raised requests and of ``role-signal-clear``
events; `fa orch signal-answer` is the only writer of ``role-signal-answer`` events.
Raise times and the facts each raise saw (``facts`` on the request) come from the inbox
records themselves, so a gate restart keeps the same rule (#1155). The re-raise decision reads
only the request record, never the 30-day journal: `fa orch signal-answer` sets ``answer`` in the
same locked step that marks the request done, and the tick sets ``cleared_at`` on the request
when its signal goes unmet (it also appends the ``role-signal-clear`` event for the record).

Gate alarms (#1469): on a tick that may start work, the gate calls a role of ``GATE_CALLED``
directly for its met signal (and for ``orch_facts_kinds.ALARM_KINDS``) instead of asking the
orchestrator. The request stays open until that session finishes its dive, which records it
``called`` (``complete_call``). After any answer it calls again only when the burden is worse
than the facts stored at that answer (``answer_facts``, measured by `fa orch signal-answer`);
the same or better numbers, a clear and return, or elapsed time alone stay quiet.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from foragent import orch_events, orch_facts_kinds, orch_inbox, orch_roles
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
# Roles the gate calls itself for their met signals; the orchestrator receives the result.
GATE_CALLED = ("architect",)
# A gate call whose session ended without finishing its dive is tried again after this long,
# as a failed scheduled role session is (orch_role_schedule.RETRY_AFTER).
RETRY_AFTER = timedelta(hours=1)
ALARM_SIGNAL = {"paths": [], "kinds": list(orch_facts_kinds.ALARM_KINDS)}


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
    return (f"Role signal for {name}: {json.dumps(source(signal))} is met ({_parts(signal, files, kinds, numbers)}). "
            f"Call the role (fa orch {name} or its call command) and answer `fa orch signal-answer <id> called "
            f"--why ...`, or answer `not-now --why <reason>`.")


def fingerprint(signal: dict[str, Any], files: set[str], kinds: set[str], numbers: dict[str, Any],
                binding: dict[str, Any] | None) -> dict[str, Any]:
    """The facts a signal is about, never the observation time (#1155).

    The matched files of its paths and, for each met kind, that kind's facts numbers plus the
    shared facts it is about (``orch_facts_kinds.SHARED_FACTS``): for the binding constraint,
    only its kind and whether it is normal (its detail text churns). A kind never carries
    another kind's facts, so a change elsewhere leaves a not-now signal quiet.
    """
    shared = {"binding": binding and {"kind": binding.get("kind"), "normal": binding.get("normal")}}
    facts: dict[str, Any] = {}
    if signal.get("paths"):
        facts["files"] = sorted(orch_roles.matching_files(signal, files))
    if signal.get("kinds"):
        facts["kinds"] = {kind: {"numbers": numbers.get(kind),
                                 **{name: shared[name] for name in orch_facts_kinds.SHARED_FACTS.get(kind, ())}}
                          for kind in signal["kinds"] if kind in kinds}
    return json.loads(json.dumps(facts, sort_keys=True, default=str))


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True, default=str)


def burden_numbers(kind: str, numbers: Any) -> Any:
    """One kind's numbers as a burden: shares added, elapsed and context numbers left out."""
    if not isinstance(numbers, dict):
        return numbers
    numbers = dict(numbers)
    for name, (part, whole) in orch_facts_kinds.SHARE_FACTS.get(kind, {}).items():
        if isinstance(numbers.get(part), (int, float)) and numbers.get(whole):
            numbers[name] = numbers[part] / numbers[whole]
    for field in (*orch_facts_kinds.ELAPSED_FACTS.get(kind, ()), *orch_facts_kinds.CONTEXT_FACTS.get(kind, ())):
        numbers.pop(field, None)
    return numbers


def burden(facts: dict[str, Any]) -> dict[str, Any]:
    """The burden view of request facts, for comparisons only (#1469).

    It leaves out the numbers that grow with time alone (``ELAPSED_FACTS``) and those that only
    give scale (``CONTEXT_FACTS``), adds the shares ``SHARE_FACTS`` derives, and shows a normal
    binding constraint as none, so a return to normal is never a new burden.
    """
    result = json.loads(json.dumps(facts, sort_keys=True, default=str))
    for kind, item in (result.get("kinds") or {}).items():
        if not isinstance(item, dict):
            continue
        item["numbers"] = burden_numbers(kind, item.get("numbers"))
        if isinstance(item.get("binding"), dict):
            item["binding"] = None if item["binding"].get("normal") else item["binding"].get("kind")
    return result


def worse(before: Any, after: Any) -> bool:
    """Whether ``after`` holds a burden ``before`` did not: a greater number, a new member or
    key, or a different value. Order, smaller numbers and missing members or values are not worse."""
    if after is None:
        return False
    if isinstance(after, dict) and isinstance(before, dict):
        return any(key not in before or worse(before[key], value) for key, value in after.items())
    if isinstance(after, list) and isinstance(before, list):
        seen = {json.dumps(item, sort_keys=True, default=str) for item in before}
        return any(json.dumps(item, sort_keys=True, default=str) not in seen for item in after)
    numeric = (int, float)
    if (isinstance(after, numeric) and isinstance(before, numeric)
            and not isinstance(after, bool) and not isinstance(before, bool)):
        return after > before
    return not _same(before, after)


class _Burden:
    """Unprojected numbers in ``evidence``: compared by ``worse``, not by equality."""

    def __init__(self, numbers: Any) -> None:
        self.numbers = numbers


def evidence(facts: dict[str, Any]) -> dict[tuple[str, ...], Any]:
    """Project raw request facts into affected entities and relevant constraints.

    This also reads pre-upgrade fingerprints, without changing persisted records.
    Unprojected kinds (including lane_concentration) keep their burden numbers
    (``burden_numbers``), and only a worse burden counts as new for them (#1469).
    """
    result: dict[tuple[str, ...], Any] = {}
    if "files" in facts:
        result[("files",)] = frozenset(facts["files"])
    for kind, item in facts.get("kinds", {}).items():
        numbers = item.get("numbers") or {}
        result[(kind,)] = True
        if kind in orch_facts_kinds.ENTITY_FACTS:
            for field in orch_facts_kinds.ENTITY_FACTS[kind]:
                result[(kind, field)] = frozenset(numbers.get(field) or ())
            for field in orch_facts_kinds.VALUE_FACTS.get(kind, ()):
                result[(kind, field)] = numbers.get(field)
        else:
            result[(kind, "numbers")] = _Burden(burden_numbers(kind, numbers))
        for field in orch_facts_kinds.SHARED_FACTS.get(kind, ()):
            result[(kind, field)] = item.get(field)
    return result


def new_evidence(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Shrinking sets stay quiet; compare every tick against the last raise."""
    before = evidence(previous)
    for key, value in evidence(current).items():
        if key not in before:
            return True
        if isinstance(value, frozenset):
            if value - before[key]:
                return True
        elif isinstance(value, _Burden):
            if not isinstance(before[key], _Burden) or worse(before[key].numbers, value.numbers):
                return True
        elif not _same(before[key], value):
            return True
    return False


def _signals(name: str, entry: dict[str, Any], config) -> list[dict[str, Any]]:
    """A role's configured signals, plus the gate alarms for a configured gate-called role."""
    signals = list(entry["signals"])
    if name in GATE_CALLED and name in config.roles and ALARM_SIGNAL not in signals:
        signals.append(ALARM_SIGNAL)
    return signals


def _parts(signal: dict[str, Any], files: set[str], kinds: set[str], numbers: dict[str, Any]) -> str:
    parts = []
    if signal.get("paths") and (matched := orch_roles.matching_files(signal, files)):
        parts.append(f"{len(matched)} changed file(s) in the last 24 h match {', '.join(signal['paths'])}")
    parts += [f"fact {kind}: {json.dumps(numbers[kind], sort_keys=True)}" if numbers.get(kind) else f"fact {kind}"
              for kind in signal.get("kinds") or [] if kind in kinds]
    return "; ".join(parts)


def _gate_call(store: StateStore, repo_root: Path, config, now: datetime, name: str,
               request: dict[str, Any], facts: dict[str, Any], parts: str) -> dict[str, Any]:
    """Start the role's session for the gate's open ``request``; return the request.

    The request stays open until that session finishes its dive (``complete_call``), so a
    refused or failed launch, a failed or timed-out session, or one that never reports leaves
    the alarm pending for a later start tick. ``call_facts`` are what this call is about.
    """
    from foragent import orch_architect

    request = orch_inbox.annotate_request(store, request["id"], {"call_facts": facts})
    why = (f"Gate alarm {request['id']}: {json.dumps(source(request['signal']))} is met ({parts}). "
           "Pick the worst real case behind it, follow it in time order and dig to a cause you can "
           "show with records, code and a reproduction; then propose which number should move.")
    try:
        orch_architect.call(Path(repo_root), store, config, why, now, role=name, alarm=request["id"])
    except Exception as exc:  # noqa: BLE001 - a refused or failed launch keeps the alarm pending.
        print(text("tick_step_failed", config.language, step=f"role_alarm {name}",
                   error=f"{type(exc).__name__}: {exc}"), file=sys.stderr, flush=True)
        return request
    return orch_inbox.annotate_request(store, request["id"], {"launched_at": now.isoformat()})


def complete_call(store: StateStore, request_id: str, repo_root: Path | None = None) -> None:
    """Mark a gate alarm called once its session finished.

    Like `fa orch signal-answer`, the answer keeps the signal's facts measured now, at the
    answer (``measure``), so a later call compares with what the finished dive left; the
    launch's ``call_facts`` stand in only when they cannot be read.
    """
    measured = measure(store, repo_root, request_id) if repo_root is not None else None

    def called(record: dict[str, Any]) -> None:
        if record.get("status") == "done":
            raise orch_inbox.InboxError(f"request {request_id} is already done")
        record.update(answer="called",
                      answer_facts=measured if measured is not None else record.get("call_facts") or record.get("facts"))

    try:
        record = orch_inbox.set_request_status(store, request_id, "done",
                                               "role signal called: the gate's session finished", check=called)
    except orch_inbox.InboxError:
        return  # Answered meanwhile, or no longer known: that answer stands.
    orch_events.append_event(store, store._now(), None, ANSWER, f"{request_id} {record['role']} called: gate alarm",
                             request_id=request_id, role=record["role"], answer="called", why="gate alarm",
                             signal=record["signal"])


def _gate_due(store: StateStore, now: datetime, name: str, key: str, previous: dict[str, Any] | None,
              answer: dict[str, Any] | None, facts: dict[str, Any]) -> dict[str, Any] | None:
    """The open request a gate call answers, ``{}`` for a new one, or None when no call is due."""
    from foragent import orch_architect

    baseline = (answer.get("answer_facts") or answer.get("facts")) if answer else None
    if baseline is not None and not worse(burden(baseline), burden(facts)):
        return None  # The same or better than at the last answer.
    if orch_architect.session_alive(orch_architect.read_record(store, name)):
        return None  # The running session already digs; never start a second one.
    pending = previous if previous is not None and previous.get("status") != "done" else None
    if pending is None:
        return {}
    launched = _time(pending.get("launched_at"))
    return pending if launched is None or now - launched >= RETRY_AFTER else None


def raise_due(store: StateStore, repo_root: Path, now: datetime, start: bool = False) -> list[dict[str, Any]]:
    """Raise every met role signal that is due; return the inbox requests added.

    A signal (role plus its {paths, kinds} item) is never raised while an earlier request
    for it is not done. Once that request is done, it is raised again when a tick saw it unmet
    since that raise, or its facts contain new evidence beyond the last raise, regardless
    of its answer. Time alone is not evidence. Only legacy requests without stored facts
    use the cooldown.

    With ``start`` the gate calls a ``GATE_CALLED`` role itself instead: when the signal was
    never answered, or its burden is worse than at its last answer. A clear and return, a
    restart or elapsed time alone never call. An open request of the signal, raised by a
    no-start tick or kept by a launch that did not finish, is the one the call answers; it is
    retried after ``RETRY_AFTER`` once a launched session is gone. A live session of the role
    is never duplicated.
    """
    if not (Path(repo_root) / ".foragent.yml").is_file():
        return []  # Not a Foragent repository: no roles source, nothing to measure.
    from foragent import orch_architect

    config = load_config(Path(repo_root))
    roles = {name: {**entry, "signals": _signals(name, entry, config)} for name, entry in load_roles(config).items()}
    files, kinds, numbers, binding = orch_roles.observe(store, config, Path(repo_root), roles)
    last: dict[str, dict[str, Any]] = {}
    answered: dict[str, dict[str, Any]] = {}
    for row in signal_requests(store):
        last[_key(row["role"], row["signal"])] = row
        if row.get("status") == "done":
            answered[_key(row["role"], row["signal"])] = row
    clears, added = _clears(store), []
    for name, entry in roles.items():
        gate = start and name in GATE_CALLED and name in config.roles
        for signal in entry["signals"]:
            key, previous = _key(name, signal), last.get(_key(name, signal))
            raised_at = _time(previous.get("created_at")) if previous else None
            cleared = previous is not None and (bool(previous.get("cleared_at")) or (
                raised_at is not None and any(at > raised_at for at in clears.get(key, []))))
            if not orch_roles.signal_met(signal, files, kinds):
                if previous is not None and not cleared:
                    last[key] = orch_inbox.annotate_request(store, previous["id"], {"cleared_at": now.isoformat()})
                    orch_events.append_event(store, now, None, CLEAR, f"{name}: signal cleared",
                                             role=name, signal=source(signal))
                    # A repeated identical signal sees the cleared request and clears once.
                continue
            facts = fingerprint(signal, files, kinds, numbers, binding)
            if gate:
                request = _gate_due(store, now, name, key, previous, answered.get(key), facts)
                if request is None:
                    continue
                if not request:
                    request = orch_inbox.add_request(store, "gate", _text(name, signal, files, kinds, numbers),
                                                     {"role": name, "signal": source(signal), "facts": facts})
                    added.append(request)
                last[key] = _gate_call(store, repo_root, config, now, name, request, facts,
                                       _parts(signal, files, kinds, numbers))
                continue
            if previous is not None:
                if previous.get("status") != "done":
                    continue
                if "facts" in previous:
                    due = cleared or new_evidence(previous["facts"], facts)
                else:
                    due = cleared or raised_at is None or now - raised_at >= cooldown(repo_root)
                if not due:
                    continue
            last[key] = orch_inbox.add_request(store, "gate", _text(name, signal, files, kinds, numbers),
                                               {"role": name, "signal": source(signal), "facts": facts})
            added.append(last[key])  # A repeated identical signal sees this open request.
    return added


def tick(store: StateStore, repo_root: Path, now: datetime, language: str,
         start: bool = False) -> list[dict[str, Any]]:
    """The gate tick's role-signal step; a failure is logged and never fails the tick.

    ``start`` is whether this tick may start work; only then does the gate call a role itself.
    """
    with step("role_signals"):
        try:
            return raise_due(store, repo_root, now, start=True) if start else raise_due(store, repo_root, now)
        except Exception as exc:  # noqa: BLE001 - one failed check must not stop the tick.
            print(text("tick_step_failed", language, step="role_signals", error=f"{type(exc).__name__}: {exc}"),
                  file=sys.stderr, flush=True)
            return []


def answer(store: StateStore, request_id: str, how: str, why: str,
           facts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record the orchestrator's answer to one role-signal request and mark it done.

    ``facts`` are the signal's facts measured at the answer (``measure``); a later gate call
    compares against them, not against the facts the raise saw (#1469).

    The not-done check and the done mark are one step under the inbox state lock, so of two
    concurrent answers only one succeeds; the other fails and appends no answer event.
    """
    if how not in ANSWERS:
        raise orch_inbox.InboxError(f"answer must be one of: {', '.join(ANSWERS)}")
    why = (why or "").strip()
    if how == "not-now" and not why:
        raise orch_inbox.InboxError("not-now needs a reason in --why")

    def open_signal(record: dict[str, Any]) -> None:
        if not is_signal_request(record):
            raise orch_inbox.InboxError(f"request {request_id} is not a role signal")
        if record.get("status") == "done":
            raise orch_inbox.InboxError(f"request {request_id} is already done")
        record["answer"] = how  # Set with the done mark, so a tick never sees done without it.
        if facts is not None:
            record["answer_facts"] = facts

    record = orch_inbox.set_request_status(store, request_id, "done",
                                           f"role signal {how}: {why}" if why else f"role signal {how}",
                                           check=open_signal)
    return orch_events.append_event(store, store._now(), None, ANSWER, f"{request_id} {record['role']} {how}: {why}",
                                    request_id=request_id, role=record["role"], answer=how, why=why,
                                    signal=record["signal"])


def add_parser(orch_actions) -> None:
    parser = orch_actions.add_parser("signal-answer", help="Answer one role-signal request: called or not-now")
    parser.add_argument("id", help="Request id of the role signal")
    parser.add_argument("answer", choices=ANSWERS)
    parser.add_argument("--why", default="", help="Why; required for not-now")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())


def measure(store: StateStore, repo_root: Path, request_id: str) -> dict[str, Any] | None:
    """The facts of one raised request's signal now; None when they cannot be read."""
    record = next((row for row in signal_requests(store) if row["id"] == request_id), None)
    if record is None or not (Path(repo_root) / ".foragent.yml").is_file():
        return None
    try:
        config = load_config(Path(repo_root))
        signal = record["signal"]
        files, kinds, numbers, binding = orch_roles.observe(store, config, Path(repo_root),
                                                            {record["role"]: {"signals": [signal]}})
    except Exception:  # noqa: BLE001 - the answer itself must still be recorded.
        return None
    return fingerprint(signal, files, kinds, numbers, binding)


def command(args, store: StateStore) -> None:
    try:
        event = answer(store, args.id, args.answer, args.why, measure(store, args.repo_root.resolve(), args.id))
    except orch_inbox.InboxError as exc:
        raise SystemExit(f"fa orch signal-answer: {exc}") from None
    print(f"Role {event['role']}: signal {args.id} answered {args.answer}.")
