"""Refuse a retry that would repeat a same-class review hold.

The gate holds an issue with ``review-defects`` after two failed rounds of the same
defect class. Another implementation of the same contract only repeats that failure, so
`fa orch retry` refuses until an approved order edit changes the contract.

The contract is content, never time: the approval's Goal section and Done when commands
(hashed) and its locked acceptance test blobs. A re-approval, a later ``approved_at``, or
an edit of other sections (Notes, Preparation) changes none of them. ``record_hold`` is the
one writer: it stores the contract of the approval in force when the hold is flagged
(the approval the held run used), in ``<state>/hold-contracts/<n>.json``.
"""

from __future__ import annotations

import hashlib
import json

from foragent.issue import parse_body
from foragent.reasons import Reason

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


def refusal(ctx, n: int) -> str | None:
    """The refusal text while the same-class hold stands on an unchanged approved contract."""
    flag = ctx.store.load_flag(n)
    if flag is None or flag.reason != Reason.REVIEW_DEFECTS:
        return None
    approval = ctx.store.load_approval(n)
    held = _held_contract(ctx.store, n)
    if approval is not None and held is not None and contract(approval) != held:
        return None  # An approved order edit changed the Goal, Done when or locked tests.
    return REFUSAL.format(n=n)
