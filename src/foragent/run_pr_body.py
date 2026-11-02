"""Compose PR bodies from values; rendered Markdown is an output only."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from types import SimpleNamespace
from typing import Self

from foragent.risks import NOTES_END, NOTES_START
from foragent.run_attempt import ExecutorStopped
from foragent.run_prompts import DECISIONS_END, DECISIONS_START, review_scope_rules
from foragent.tracker import TrackerError, pr_issue_number

LIMIT = 65536
COMMENT_CHUNK = 60000


@dataclass
class Block:
    """Optional text: whole in the body, or one note naming the comments that hold it."""

    label: str
    text: str

    @property
    def comment_name(self) -> str:
        digest = sha256(self.text.encode("utf-8", "surrogatepass")).hexdigest()[:12]
        return f"Foragent full {self.label} {digest}"

    @property
    def note(self) -> str:
        return f"Removed {self.label}; full text: PR comment `{self.comment_name}` on this PR."


class Body(str):
    """A composed body, carrying the removed blocks its notes promise as named comments."""

    comments: list[tuple[str, str]]

    def __new__(cls, text: str, comments: list[tuple[str, str]] | None = None) -> Self:
        body = super().__new__(cls, text)
        body.comments = comments or []
        return body


def compose(parts: list[str | Block]) -> Body:
    """Join required text and optional blocks under one budget, deciding before rendering.

    A complete body that fits is returned as it is. Otherwise every block keeps its note's
    room and an equal share of what remains, whatever the other blocks hold, so no block's
    length can remove another block or a required value.
    """
    blocks = [part for part in parts if isinstance(part, Block)]

    def render(values: dict[int, str]) -> str:
        return "\n".join(values[id(part)] if isinstance(part, Block) else part for part in parts)

    whole = render({id(block): block.text for block in blocks})
    if len(whole) <= LIMIT:
        return Body(whole)
    required = len(render({id(block): "" for block in blocks}))
    notes = sum(len(block.note) for block in blocks)
    if required + notes > LIMIT:
        raise ExecutorStopped(
            "pr-body-too-long",
            f"Required PR content is {required} characters and its {notes} characters of omission notes "
            f"do not fit the limit of {LIMIT}; no PR body was written.",
        )
    share = (LIMIT - required - notes) // max(1, len(blocks))
    removed = [block for block in blocks if len(block.text) > share + len(block.note)]
    values = {id(block): block.text for block in blocks} | {id(block): block.note for block in removed}
    return Body(render(values), [(block.comment_name, block.text) for block in removed])


def publish_comments(ctx, pr: int, body: str) -> None:
    """Post what a body's notes name; the record holds the source if a comment cannot be sent."""
    comments = getattr(body, "comments", [])
    if not comments:
        return
    try:
        posted = {comment.body.split("\n", 1)[0] for comment in ctx.tracker.list_comments(pr)}
        for name, text in comments:
            chunks = range(0, len(text), COMMENT_CHUNK)
            for number, start in enumerate(chunks, 1):
                header = f"{name} [part {number}/{len(chunks)}]"
                if header not in posted:
                    ctx.tracker.comment(pr, f"{header}\n{text[start:start + COMMENT_CHUNK]}")
    except TrackerError as exc:
        if exc.waits:
            raise
        from foragent.orch_events import append_event

        append_event(ctx.store, ctx.now(), pr, "pr-full-text-not-retained",
                     f"PR #{pr} is missing full-text comments; pr-content holds the source: {exc}")


def _pr_body(n, parsed, checks, attempt, reviews, try_text=None, *, outside_lane=None,
             fallback_model=None, attempt_tiers=None, missing_skills=None, skills_line=None,
             rebuttals="", searched="", review_scope="claude-local", acceptance_dir="tests/acceptance/",
             risk_notes="", language="en", lane_decisions="", summary="", approval_line="") -> Body:
    lines = [f"Closes #{n}", "", "## Summary", "", summary, "", approval_line, "",
             "| Done when command | Result |", "| --- | --- |"]
    rows = []
    for check in checks:
        command = check.command.replace("&", "&amp;").replace("<", "&lt;").replace("|", "&#124;")
        command = command.replace("`", "&#96;").replace("\n", "<br>")
        passed = check.exit_code == 0 and not check.timed_out
        rows.append(f"| <code>{command}</code> | {'pass' if passed else 'fail'} |")
    if rows:
        lines.append(Block("check table", "\n".join(rows)))
    lines.extend(["", "## Review", ""])
    if review_scope == "pr-body":
        lines.append(f"VERDICT: {reviews[-1].verdict}" if reviews else "Pending review.")
        lines.extend(["", "## Review scope", "", review_scope_rules(acceptance_dir, language),
                      "The acceptance tests are locked."])
        if risk_notes:
            lines.extend(["", NOTES_START, Block("risk notes", risk_notes), NOTES_END])
        if lane_decisions:
            lines.extend(["", DECISIONS_START, Block("lane decisions", lane_decisions), DECISIONS_END])
    elif not reviews:
        lines.append("Disabled (reviewer: none).")
    for number, review in enumerate(reviews if review_scope == "claude-local" else [], 1):
        lines.append(f"Round {number}: VERDICT: {review.verdict}")
        if review.findings:
            lines.append(Block(f"review {number} findings", review.findings))
        if review.review_notes:
            lines.append(Block(f"review {number} notes", "\n".join(f"NOTE: {note}" for note in review.review_notes)))
        if review.notes:
            lines.append(Block(f"review {number} details", "\n".join(review.notes)))
        lines.append("")
    for label, heading, content in (("rebuttals", "Rebuttals", rebuttals), ("evidence", "Searched", searched)):
        if content:
            lines.extend(["", f"## {heading}", "", Block(label, content)])
    if parsed.done_when_checklist:
        lines.extend(["", "## Needs a human check", ""])
        lines.extend(f"- [ ] {item.text}" for item in parsed.done_when_checklist)
    if outside_lane:
        lines.extend(["", "## Outside lane", "", Block("outside lane", "\n".join(f"- `{p}`" for p in outside_lane))])
    if missing_skills:
        lines.extend(["", "## Lane skills", "", Block("missing skills", "\n".join(f"- missing skill: {name}" for name in missing_skills))])
    if skills_line is not None:
        lines.extend(["", skills_line])
    if fallback_model is not None:
        lines.extend(["", f"Executor fallback model: {fallback_model}"])
    if attempt_tiers:
        lines.extend(["", *(f"Attempt {number} at tier {tier}" for number, tier in attempt_tiers.items())])
    lines.extend(["", f"Opened by Foragent gate (attempt {attempt})", "", "## Try it", "",
                  Block("try instructions", try_text if try_text is not None else "_not provided by the implementer_")])
    return compose(lines)


def check_values(checks) -> list[dict]:
    """What a check row shows; the command output stays in the run's log."""
    return [{"command": check.command, "exit_code": check.exit_code, "timed_out": check.timed_out} for check in checks]


def review_values(reviews) -> list[dict]:
    """What a review contributes to the body; the raw reviewer text stays in the run's artifacts."""
    return [{"verdict": review.verdict, "findings": review.findings, "notes": list(review.notes),
             "review_notes": list(review.review_notes)} for review in reviews]


def _values_path(n: int, ctx):
    return ctx.store.root / "pr-content" / f"{n}.json"


def save_values(n: int, ctx, values: dict) -> None:
    """The gate's own record of a PR's composition; only these functions write it."""
    path = _values_path(n, ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values), encoding="utf-8")
    temporary.replace(path)


def read_values(n: int, ctx) -> dict | None:
    path = _values_path(n, ctx)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _compose_values(n: int, ctx, values: dict) -> Body:
    from foragent.issue import parse_body

    approval = ctx.store.load_approval(n)
    if approval is None:
        raise ExecutorStopped("pr-body-values-missing", f"No approval record for #{n}; no PR body was written.")
    return _pr_body(
        n, parse_body(approval.body), **{
            **values,
            "checks": [SimpleNamespace(**check) for check in values.get("checks", [])],
            "reviews": [SimpleNamespace(**review) for review in values.get("reviews", [])],
            "approval_line": f"Approval: {approval.approved_by} at {approval.approved_at}",
        },
    )


def render_values(n: int, ctx, values: dict) -> Body:
    """Compose a body to send; a body that cannot be composed stops here and reaches a person."""
    from foragent.actions import flag
    from foragent.reasons import Reason

    try:
        return _compose_values(n, ctx, values)
    except ExecutorStopped as exc:
        flag(ctx, n, Reason.RETRIES_EXCEEDED, str(exc), needs_human=True)
        raise


def load_values(n: int, ctx) -> dict:
    """The record a body update starts from; a PR with none stops before any write."""
    from foragent.actions import flag
    from foragent.reasons import Reason

    values = read_values(n, ctx)
    if values is None:
        detail = f"PR for #{n} has no pr-content record, so no PR body was written."
        flag(ctx, n, Reason.RETRIES_EXCEEDED, detail, needs_human=True)
        raise ExecutorStopped("pr-body-values-missing", detail)
    return values


def merge_retitle_body(head: str, ctx) -> Body | None:
    """The body for a retitle just before merge; None means merge with the subject and write nothing."""
    number = pr_issue_number(head)
    if number is None:
        return None
    if head.startswith("foragent/batch-"):
        return batch_body(number, ctx)
    values = read_values(number, ctx)
    if values is None:
        return None
    try:
        return _compose_values(number, ctx, values)
    except ExecutorStopped:
        return None


def batch_body(parent: int, ctx) -> Body:
    """Children and merged PRs come from the tracker; no closing keyword, so completion stays with
    the gate's order aggregation (board_parents)."""
    from foragent.i18n import text

    branch = f"foragent/batch-{parent}"
    prs = {pr.head: pr for pr in ctx.tracker.list_prs("merged", "foragent/issue-") if pr.base == branch}
    children = []
    for child in sorted(ctx.tracker.sub_issues(parent), key=lambda c: c.number):
        pr = prs.get(f"foragent/issue-{child.number}")
        children.append(f"- #{child.number}: {child.title}" + (f" (PR #{pr.number})" if pr else ""))
    return compose([text("batch_pr_parent", ctx.config.language, parent=parent), "", "## Children", "",
                    Block("children", "\n".join(children))])
