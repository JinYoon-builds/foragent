"""A person's first-hand approval of a plan, recorded as an append-only fact (r1265).

Planning records the approved scope (plan id, each issue key with its goal), the person's
words and the time with `fa order approval PLAN.json --words TEXT`. A later
`fa order publish PLAN.json --approve --recorded-approval` approves only what the latest
record of that plan covers: each approved key must be in it with the same goal; fewer keys
are covered. Approval relayed as free text is not a record and grants nothing.
"""

from datetime import UTC, datetime
import hashlib
import json

from foragent.i18n import text
from foragent.order import PlanError
from foragent import order_notes

JOURNAL = "plan-approvals.jsonl"
MARKER = "<!-- foragent:recorded-approval {id} -->"


def _scope(plan: dict, language: str) -> dict[str, str]:
    """Each issue key, and each edit as ``#N``, with its goal, as the plan states them now."""
    if not isinstance(plan, dict) or not isinstance(plan.get("id"), str) or not plan["id"].strip():
        raise PlanError(text("order_approval_plan_invalid", language))
    scope = {}
    for item in [*plan.get("issues", []), *plan.get("edits", [])]:
        key = item.get("key") if "key" in item else f"#{item.get('number')}"
        if not isinstance(key, str) or not isinstance(item.get("goal"), str):
            raise PlanError(text("order_approval_plan_invalid", language))
        scope[key] = item["goal"]
    if not scope:
        raise PlanError(text("order_approval_plan_invalid", language))
    return scope


def record(plan: dict, store, words: str, language: str = "en") -> dict:
    if not words.strip():
        raise PlanError(text("order_approval_words_empty", language))
    row = {"plan": plan["id"] if isinstance(plan, dict) else None, "issues": _scope(plan, language),
           "words": words, "recorded_at": datetime.now(UTC).replace(microsecond=0).isoformat()}
    row["id"] = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    order_notes.append_entry(store, row, journal=JOURNAL, language=language)
    return row


def latest(plan: dict, store, language: str = "en") -> dict:
    """The latest record of this plan; whether it covers a publication is judged on its selection."""
    _scope(plan, language)
    rows = [row for row in order_notes.entries(store, JOURNAL) if row.get("plan") == plan["id"]]
    if not rows:
        raise PlanError(text("order_approval_missing", language, plan=plan["id"]))
    return rows[-1]


def require_covered(plan: dict, approval: dict, keys: set[str], edits: set[int], language: str = "en") -> None:
    """Refuse unless every child key and edit this publication approves is recorded with its goal."""
    scope = _scope(plan, language)
    targets = sorted(keys) + [f"#{number}" for number in sorted(edits)]
    if not targets:
        raise PlanError(text("order_approval_needs_approve", language))
    recorded = approval.get("issues") or {}
    uncovered = [key for key in targets if key not in recorded]
    differs = [key for key in targets if key in recorded and recorded[key] != scope[key]]
    if uncovered or differs:
        lines = [text("order_approval_uncovered", language, issue=key) for key in uncovered]
        lines += [text("order_approval_goal_differs", language, issue=key) for key in differs]
        raise PlanError("\n".join([text("order_approval_refused", language, plan=plan["id"]), *lines]))


def show(tracker, approval: dict | None, numbers, language: str) -> None:
    """Comment the recorded words and time on each approved issue once, before it is approved."""
    if approval is None:
        return
    marker = MARKER.format(id=approval["id"])
    for number in sorted(numbers):
        if any(marker in (comment.body or "") for comment in tracker.list_comments(number)):
            continue
        tracker.comment(number, "\n".join([
            text("order_approval_evidence", language, at=approval["recorded_at"]),
            "", *("> " + line for line in approval["words"].splitlines()), "", marker]))


def run(args, root) -> int:
    from foragent.commands import facade, repo_store

    cli = facade()
    config, _repo, store = repo_store(root)
    try:
        row = record(cli.order.read_plan(args.plan), store, args.words, config.language)
    except OSError as exc:
        raise PlanError(text("order_approval_io", config.language, error=exc)) from exc
    print(json.dumps(row, ensure_ascii=False) if args.json else
          text("order_approval_saved", config.language, plan=row["plan"], count=len(row["issues"]),
               at=row["recorded_at"]))
    return 0
