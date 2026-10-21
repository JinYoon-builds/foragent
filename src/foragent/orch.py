"""Orchestrator actions and their local/GitHub audit trail."""

from collections import deque
from datetime import UTC, datetime
import json
import re
from typing import Any

from foragent.labels import BLOCKED, NEEDS_HUMAN, issue_lane
from foragent.runner import ReviewResult, RunContext, review_only


class OrchError(ValueError):
    """An orchestrator action cannot be applied to the current state."""


def _text(value: str, name: str) -> str:
    if not value.strip():
        raise OrchError(f"{name} must not be empty")
    return value.strip()


def _issue(ctx: RunContext, number: int) -> None:
    issue = ctx.tracker.get_issue(number)
    if issue.is_pull_request or issue.state != "open":
        raise OrchError(f"#{number} must be an open issue")


def _idle(ctx: RunContext, number: int) -> None:
    _issue(ctx, number)
    run = ctx.store.get_run(number)
    if run is not None and run.status == "running":
        raise OrchError(f"issue #{number} already has an active runner")


def record_event(ctx: RunContext, number: int | None, kind: str, summary: str) -> dict[str, Any]:
    """Mirror issue actions to GitHub; list/log events have no issue target."""
    event = {"at": ctx.now().astimezone(UTC).isoformat(), "issue": number,
             "kind": kind, "summary": summary}
    with ctx.store._state_lock():
        with (ctx.store.root / "orch-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    if number is not None:
        ctx.tracker.comment(number, f"<!-- foragent:orch -->\n{summary}")
    return event


def list_blocked(ctx: RunContext) -> list[dict[str, Any]]:
    items = []
    for issue in sorted(ctx.tracker.list_open_issues(), key=lambda issue: issue.number):
        if BLOCKED not in issue.labels:
            continue
        comments = []
        for comment in ctx.tracker.list_comments(issue.number):
            marker = re.fullmatch(r"<!-- foragent:blocked reason=([a-z-]+) -->",
                                  comment.body.partition("\n")[0].removesuffix("\r"))
            if marker:
                comments.append((comment, marker[1]))
        latest = max(comments, key=lambda item: (datetime.fromisoformat(item[0].created_at), item[0].id),
                     default=None)
        reviews = [{"path": str(path), "text": path.read_text(encoding="utf-8", errors="replace")}
                   for path in sorted((ctx.store.root / "runs" / f"issue-{issue.number}").glob("review-*.txt"))]
        log = ctx.store.log_path(issue.number)
        tail = []
        if log.is_file():
            with log.open(encoding="utf-8", errors="replace") as stream:
                tail = list(deque(stream, maxlen=60))
        items.append({
            "number": issue.number, "title": issue.title, "lane": issue_lane(issue.labels, ctx.config.lanes),
            "reason": latest[1] if latest else None,
            "blocked_at": latest[0].created_at if latest else None,
            "details": latest[0].body.partition("\n")[2] if latest else "",
            "reviews": reviews, "log_path": str(log), "log_tail": "".join(tail),
        })
    record_event(ctx, None, "list", f"Listed {len(items)} blocked issue(s).")
    return items


def rerun_review(ctx: RunContext, number: int) -> str:
    _idle(ctx, number)
    outcome = "error"
    try:
        outcome = review_only(number, ctx)
    finally:
        record_event(ctx, number, "rerun-review", f"#{number}: reran the PR review; {outcome}.")
    return outcome


def _retry(ctx: RunContext, number: int, hint: str | None) -> None:
    if (pr := ctx.tracker.find_pr(f"foragent/issue-{number}")) is not None:
        ctx.tracker.close_pr(pr)
    path = ctx.store.root / "hints" / f"{number}.txt"
    if hint is not None:
        ctx.store._atomic_write(path, hint)
    else:
        path.unlink(missing_ok=True)
    ctx.tracker.remove_labels(number, [BLOCKED])


def retry(ctx: RunContext, number: int, hint: str | None = None) -> None:
    _idle(ctx, number)
    hint = _text(hint, "hint") if hint is not None else None
    _retry(ctx, number, hint)
    summary = f"#{number}: requested an implementation retry." + (f" Hint: {hint}" if hint else "")
    record_event(ctx, number, "retry", summary)


def resolve(ctx: RunContext, number: int, verdict: str, note: str) -> str:
    _idle(ctx, number)
    if verdict not in {"pass", "fail"}:
        raise OrchError("verdict must be pass or fail")
    note = _text(note, "note")
    pr = ctx.tracker.find_pr(f"foragent/issue-{number}")
    if pr is None:
        raise OrchError(f"issue #{number} has no open Foragent PR")
    head = ctx.tracker.pr_head_sha(pr)
    ctx.tracker.comment(number, f"<!-- foragent:orch-verdict pr={pr} head={head} verdict={verdict} -->\n{note}")
    outcome = "error"
    try:
        if verdict == "fail":
            _retry(ctx, number, note)
            outcome = "retry"
        else:
            outcome = review_only(number, ctx, verdict=ReviewResult("PASS", note), expected_head_sha=head)
    finally:
        record_event(ctx, number, "resolve", f"#{number}: recorded {verdict} for PR #{pr} at {head}; {outcome}. {note}")
    return outcome


def escalate(
    ctx: RunContext, number: int, summary: str, tried: str,
    options: list[str] | None = None, recommend: int | None = None,
) -> None:
    _issue(ctx, number)
    summary, tried = _text(summary, "summary"), _text(tried, "tried")
    options = [_text(option, "option") for option in options or []]
    if recommend is not None and not 1 <= recommend <= len(options):
        raise OrchError("recommend must be a 1-based option index")
    lines = ["<!-- foragent:needs-human reason=escalated -->", summary, "", "What was tried:", tried]
    if options:
        lines.extend(["", "Options:"])
        lines.extend(f"{i}. {option}" + (" (recommended)" if i == recommend else "")
                     for i, option in enumerate(options, 1))
    ctx.tracker.comment(number, "\n".join(lines))
    ctx.tracker.add_labels(number, [NEEDS_HUMAN])
    record_event(ctx, number, "escalate", f"#{number}: escalated for a human decision. {summary}")


def log(ctx: RunContext) -> list[dict[str, Any]]:
    path = ctx.store.root / "orch-events.jsonl"
    events = []
    with ctx.store._state_lock():
        if path.is_file():
            with path.open(encoding="utf-8") as stream:
                events = [json.loads(line) for line in deque(stream, maxlen=20)]
    record_event(ctx, None, "log", f"Read {len(events)} orchestrator event(s).")
    return events
