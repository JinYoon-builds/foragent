"""Refuse a retry that would repeat a same-class review hold.

The gate holds an issue with ``review-defects`` after two failed rounds of the same
defect class. Another implementation of the same contract only repeats that failure, so
`fa orch retry` refuses until an approved order edit changes the contract.

The contract is content, never time: the approval's Goal section and Done when commands
(hashed) and its locked acceptance test blobs. A re-approval, a later ``approved_at``, or
an edit of other sections (Notes, Preparation) changes none of them. ``record_hold`` is the
one writer: it stores the contract of the approval in force when the hold is flagged
(the approval the held run used), in ``<state>/hold-contracts/<n>.json``.

A hold flagged before that record existed (#2458) has no file; its contract is read back
from the approval in force when it was flagged (``legacy_contract``).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from foragent.issue import body_hash, parse_body
from foragent.reasons import Reason, parse_approval_comment

REFUSAL = ("#{n} is held after two failed rounds of the same defect class; another retry "
           "would repeat the same failure, with or without --hint. Narrow, split or redesign "
           "its Goal through an order edit; once the edited Goal or acceptance tests are "
           "approved, retry works again.")


def contract(approval) -> dict:
    """What a build is held to: the Goal and Done when commands, and the locked test blobs."""
    parsed = parse_body(approval.body)
    text = json.dumps([parsed.goal, parsed.done_when_commands], ensure_ascii=False)
    return {"goal_sha": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "acceptance_blobs": dict(approval.acceptance_blobs)}


def _path(store, n: int):
    return store.root / "hold-contracts" / f"{n}.json"


def record_hold(store, n: int) -> None:
    """Keep the contract the issue was held on; called where the same-class hold is flagged."""
    approval = store.load_approval(n)
    if approval is None:
        return
    path = _path(store, n)
    path.parent.mkdir(parents=True, exist_ok=True)
    store._write_json(path, contract(approval))


def _held_contract(store, n: int) -> dict | None:
    try:
        value = json.loads(_path(store, n).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _at(text: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _approval_record(ctx, n: int, flagged: datetime) -> dict | None:
    """The approval snapshot in force at the flag, from the gate's approval comments.

    The gate posts one comment per saved approval snapshot (body hash and locked test
    blobs); the latest one approved no later than the flag is the approval the hold met."""
    records = [(at, record) for comment in ctx.tracker.list_comments(n)
               if (record := parse_approval_comment(comment.body)) is not None
               and (at := _at(record["approved_at"])) is not None and at <= flagged]
    return max(records, key=lambda item: item[0])[1] if records else None


def _held_bodies(store, n: int, flagged: datetime) -> list[str]:
    """Approved bodies the implementer sessions of runs started by the flag used, newest first."""
    runs = [run for run in [store.get_run(n), *store.read_history()]
            if run is not None and run.number == n and run.kind == "issue" and run.session_body
            and (started := _at(run.started_at)) is not None and started <= flagged]
    return [run.session_body for run in sorted(runs, key=lambda run: _at(run.started_at), reverse=True)]


def _goal_sha(body: str) -> str:
    return contract(SimpleNamespace(body=body, acceptance_blobs={}))["goal_sha"]


def legacy_contract(ctx, n: int, flag, approval) -> dict | None:
    """The contract of a hold with no record: the approval in force when it was flagged.

    An approval saved no later than the flag is that approval. A later one replaced it, so
    each part comes from what was kept from that time, never from the current approval: the
    locked blobs from the approval comment in force at the flag, the Goal and Done when from
    the body with that comment's hash (the current approval's, or the held run's), else the
    held run's body. A part nothing kept is None (unknown); nothing known at all is None."""
    flagged, approved = _at(flag.flagged_at), _at(approval.approved_at)
    if flagged is None or approved is None:
        return None
    if approved <= flagged:
        return contract(approval)
    record = _approval_record(ctx, n, flagged)
    bodies = [approval.body, *_held_bodies(ctx.store, n, flagged)]
    if record is None:
        body = bodies[1] if len(bodies) > 1 else None
    else:
        body = next((text for text in bodies if body_hash(text) == record["body_sha"]), None)
    held = {"goal_sha": None if body is None else _goal_sha(body),
            "acceptance_blobs": None if record is None else dict(record["acceptance_blobs"])}
    return held if any(value is not None for value in held.values()) else None


def changed(current: dict, held: dict) -> bool:
    """Whether a known part of the held contract differs from the current one."""
    return any(held.get(key) is not None and held[key] != current[key] for key in current)


def refusal(ctx, n: int) -> str | None:
    """The refusal text while the same-class hold stands on an unchanged approved contract."""
    flag = ctx.store.load_flag(n)
    if flag is None or flag.reason != Reason.REVIEW_DEFECTS:
        return None
    approval = ctx.store.load_approval(n)
    held = _held_contract(ctx.store, n)
    if held is None and approval is not None:
        held = legacy_contract(ctx, n, flag, approval)
    if approval is not None and held is not None and changed(contract(approval), held):
        return None  # An approved order edit changed the Goal, Done when or locked tests.
    return REFUSAL.format(n=n)
