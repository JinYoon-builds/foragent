"""Independent review execution, findings, rebuttals and review-only verification."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING

from foragent import cuts
from foragent.actions import flag, invalidate_approval
from foragent.config import resolved_reviewer
from foragent.git import GitRepo, REVIEW_SCOPE_FILE
from foragent.issue import ParsedIssue, body_hash, has_machine_check, parse_body
from foragent.labels import APPROVED
from foragent.reasons import Reason
from foragent.run_attempt import WholeFailure
from foragent.run_checks import _acceptance_matches, _append, _verify_review_head
from foragent.run_execution import _available_executor
from foragent.run_prompts import build_review_prompt, review_scope_rules
from foragent.run_waits import _phase, _progress, _timeout

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.run_attempt import Implementation


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    findings: str
    notes: tuple[str, ...] = ()
    raw: str = ""
    timed_out: bool = False


def parse_review(message: str) -> ReviewResult:
    lines = message.splitlines()
    for index, line in enumerate(lines):
        if match := re.fullmatch(r"VERDICT: (PASS|FAIL)\s*", line):
            findings = [line for line in lines[index + 1:] if line.strip()]
            return ReviewResult(match[1], "\n".join(findings))
    return ReviewResult("UNUSABLE", "", ("reviewer gave no verdict",))


def parse_code_review(message: str) -> ReviewResult:
    try:
        event = json.loads(message)
    except ValueError:
        event = None
    if not isinstance(event, dict) or event.get("is_error") or not isinstance(event.get("result"), str):
        return ReviewResult("UNUSABLE", "", ("/code-review gave no usable result",))
    text = event["result"]
    lines = [line for line in text.splitlines() if line.strip()]
    findings = [line for line in lines if re.match(r"^(?:[-*]|\d+[.)])?\s*`?\S+?:\d+`?\s+(—|-|--)\s+", line)]
    if findings:
        return ReviewResult("FAIL", "\n".join(findings))
    payload = text.strip()
    blocks = list(re.finditer(r"^[ \t]*```([^\r\n]*)\r?\n(.*?)^[ \t]*```[ \t]*\r?$", payload, re.M | re.S))
    json_blocks = [block for block in blocks if block[1].strip() == "json"]
    if len(json_blocks) > 1:
        return ReviewResult("UNUSABLE", "", ("/code-review gave multiple findings payloads",))
    if len(blocks) == 1 and blocks[0].span() == (0, len(payload)):
        payload = blocks[0][2]
    elif json_blocks:
        payload = json_blocks[0][2]
    try:
        items = json.loads(payload)
    except ValueError:
        items = None
    if isinstance(items, list):
        if not items:
            return ReviewResult("PASS", "")
        if any(not isinstance(item, dict) or not {"file", "line", "summary"}.intersection(item) for item in items):
            return ReviewResult("UNUSABLE", "", ("/code-review gave invalid findings",))
        return ReviewResult("FAIL", "\n".join(
            f"{item.get('file', '?')}:{item.get('line', '?')} — {item.get('summary', '')}" for item in items
        ))
    if not blocks and lines and lines[0].strip() == "(none)":
        return ReviewResult("PASS", "")
    return ReviewResult("UNUSABLE", "", ("/code-review gave no verdict",))


@contextmanager
def _claude_review_scope(git: GitRepo, body: str, acceptance_dir: str, rebuttals: str = ""):
    git.ignore_review_scope()
    path = git.root / REVIEW_SCOPE_FILE
    link = os.readlink(path) if path.is_symlink() else None
    original = path.read_bytes() if path.exists() else None
    mode = path.stat().st_mode if path.exists() else None
    section = (
        "# Review scope (written by Foragent for this review only)\n\n"
        f"{body}\n\n{review_scope_rules(acceptance_dir)}\n\n"
        + (f"Implementer rebuttals (judge them):\n{rebuttals}\n\n" if rebuttals else "")
    ).encode("utf-8")
    try:
        if link is not None:
            path.unlink()
        path.write_bytes(section + (original if original is not None else b""))
        yield
    finally:
        if path.is_symlink():
            path.unlink()
        if link is not None:
            path.unlink(missing_ok=True)
            path.symlink_to(link)
        elif original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
            path.chmod(mode)


def _review(n: int, ctx: RunContext, git: GitRepo, parsed: ParsedIssue, base_ref: str,
            *, round_number: int = 1, pr: int | None = None) -> ReviewResult:
    from foragent import runner

    _phase(n, ctx, "reviewing")
    reviewer = resolved_reviewer(ctx.config)
    options = {"review_level": ctx.config.review_level} if reviewer == "claude" else {}
    if reviewer == "claude" and pr is not None:
        options["review_pr"] = pr
    rebuttals = _read_rebuttals(git.root)
    edited = False
    with ExitStack() as scope:
        if reviewer == "claude" and ctx.config.review_scope == "claude-local":
            approval = ctx.store.load_approval(n)
            if approval is None:
                raise ValueError("Claude review requires an approved snapshot")
            scope.enter_context(_claude_review_scope(git, approval.body, ctx.config.acceptance_dir, rebuttals))
        before = git.review_state()
        try:
            result = _available_executor(
                n, ctx, ctx.executor_factory(reviewer), worktree=git.root,
                reviewer=True,
                prompt="" if reviewer == "claude" else build_review_prompt(
                    parsed, git.staged_diff(base_ref), ctx.config.acceptance_dir, rebuttals=rebuttals,
                    full_text_dir=cuts.issue_dir(ctx.store.root, n),
                ),
                model=ctx.config.reviewer_model, timeout_s=runner._remaining(n, ctx),
                log_path=ctx.store.log_path(n), **options,
            )
            message = result.full_message if result.full_message is not None else result.last_message
            raw = message
            if reviewer == "claude":
                try:
                    event = json.loads(message)
                except ValueError:
                    event = None
                if isinstance(event, dict) and isinstance(event.get("result"), str):
                    raw = event["result"]
            path = ctx.store.root / "runs" / f"issue-{n}" / f"review-{round_number}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(raw, encoding="utf-8")
        finally:
            if git.review_state() != before:
                git.restore_review_state(before)
                edited = True
                _append(ctx.store.log_path(n), "Reviewer changed repository state; restored HEAD, index and working tree.")
    if result.timed_out or result.exit_code != 0:
        review = ReviewResult("UNUSABLE", "", (
            f"Reviewer exit_code={result.exit_code}, timed_out={result.timed_out}",
        ))
    else:
        parser = parse_code_review if reviewer == "claude" else parse_review
        review = parser(message)
    if reviewer == "claude":
        review = ReviewResult(review.verdict, review.findings, (
            f"reviewer: /code-review {ctx.config.review_level}", *review.notes,
        ))
    if edited:
        review = ReviewResult(review.verdict, review.findings, (*review.notes, "Reviewer changes discarded; staged index restored."))
    if review.verdict == "PASS":
        ctx.store.update_run(n, last_review_pass_at=ctx.now().isoformat())
    _append(ctx.store.log_path(n), f"Review: VERDICT: {review.verdict}\n{review.findings}")
    _progress(n, ctx)
    return replace(review, raw=raw, timed_out=result.timed_out)


def _read_rebuttals(worktree: Path) -> str:
    path = worktree / ".foragent/REBUTTAL.md"
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _next_review_round(n: int, ctx: RunContext) -> int:
    rounds = [int(path.stem.removeprefix("review-"))
              for path in (ctx.store.root / "runs" / f"issue-{n}").glob("review-*.txt")
              if path.stem.removeprefix("review-").isdigit()]
    return max(rounds, default=0) + 1


def _normalized_findings(review: ReviewResult) -> set[str]:
    # Unstructured prose is one finding: repeated preambles alone are not a dispute.
    points = [review.raw] if review.verdict == "UNUSABLE" else review.findings.splitlines()
    return {" ".join(re.sub(r"^\s*[-*]\s+", "", point).split()).casefold()
            for point in points if point.strip()}


def _review_implementation(attempt: Implementation) -> None:
    from foragent.run_publish import _open_or_update_pr, _push_branch, _render_pr_body

    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    worktree = attempt.worktree
    pr = None
    if ctx.config.review_scope == "pr-body":
        title = f"{ctx.tracker.get_issue(n).title} (#{n})"
        if git.has_staged_changes():
            git.commit(title)
        _push_branch(attempt)
        pr = _open_or_update_pr(attempt, title, _render_pr_body(attempt))
        _progress(n, ctx)
    review = _review(n, ctx, git, attempt.parsed, attempt.base_ref, round_number=_next_review_round(n, ctx), pr=pr)
    if review.verdict == "UNUSABLE":
        review = replace(review, findings=review.raw)
    attempt.reviews.append(review)
    if pr is not None:
        ctx.tracker.comment(pr, "\n".join([
            f"Round {len(attempt.reviews)}: VERDICT: {review.verdict}", review.findings, *review.notes,
        ]))
    if review.verdict != "PASS":
        attempt.review_failures += 1
        if review.verdict == "UNUSABLE":
            attempt.reviewer_text = review.raw
            attempt.failure = "A reviewer said (verbatim):\n" + review.raw
        else:
            attempt.failure = WholeFailure(review.findings or "Reviewer found defects (no findings provided).")
        findings = _normalized_findings(review) if _read_rebuttals(worktree).strip() else set()
        if findings & attempt.previous_rebutted_findings:
            attempt.review_hold = True
            attempt.review_hold_reason = Reason.REVIEW_DISPUTE
        attempt.previous_rebutted_findings = findings
        if attempt.review_failures > ctx.config.review_max:
            attempt.review_hold = True


def _review_only(n: int, ctx: RunContext, worktree: Path, verdict_head: str | None) -> str:
    """Verify one immutable PR head, then apply the ordinary merge policy."""
    from foragent import runner
    from foragent.run_publish import _check_publication, _hold_changed_head, _publish_reviewed_head

    approval = ctx.store.load_approval(n)
    if approval is None:
        invalidate_approval(ctx, n, "Review requires a current approved snapshot. Re-approve to run it.")
        return "no-approval"
    pr = next((pr for pr in ctx.tracker.list_prs("open", f"foragent/issue-{n}")
               if pr.head == f"foragent/issue-{n}"), None)
    if pr is None:
        flag(ctx, n, Reason.RISK_HIGH, "No open Foragent PR to review; retry implementation.")
        return "no-pr"
    head = ctx.tracker.pr_head_sha(pr.number)
    link = f"https://github.com/{ctx.repo}/pull/{pr.number}"
    parent = ctx.tracker.parent_of(n) if "batch" in {ctx.config.review, *ctx.config.review_paths.values()} else None
    batch_branch = f"foragent/batch-{parent}" if parent is not None else None
    if pr.base not in {ctx.config.base_branch, batch_branch}:
        flag(ctx, n, Reason.RISK_HIGH, "PR targets an unexpected base; retry implementation.", link)
        return "base-changed"
    if verdict_head is not None and head != verdict_head:
        return _hold_changed_head(n, ctx, link)
    if verdict_head is not None:
        ctx.store.update_run(n, last_review_pass_at=ctx.now().isoformat())

    def approved() -> bool:
        issue = ctx.tracker.get_issue(n)
        if APPROVED not in issue.labels or body_hash(issue.body) != approval.body_sha:
            invalidate_approval(ctx, n, "Approval invalidated: issue or approval changed. Re-approve to run it.")
            return False
        return True

    if not approved():
        return "hash-mismatch"
    parsed = parse_body(approval.body)
    if not has_machine_check(parsed):
        flag(ctx, n, Reason.NO_MACHINE_CHECK, "Done when has no machine check.")
        return "no-machine-check"
    ctx.git.fetch("origin", ctx.config.base_branch)
    base_ref = f"origin/{pr.base}"
    if pr.base != ctx.config.base_branch:
        ctx.git.fetch("origin", pr.base)
    base_sha = ctx.git.ref_sha(base_ref)
    if not _acceptance_matches(n, ctx, ctx.git, approval, f"origin/{ctx.config.base_branch}"):
        return "hash-mismatch"
    ctx.git.fetch("origin", pr.head)
    if ctx.git.ref_sha(f"origin/{pr.head}") != head:
        return _hold_changed_head(n, ctx, link)
    ctx.git.add_detached_worktree(worktree, head)
    git = ctx.git.at(worktree)
    from foragent.ci_workflow import EXCLUSIONS
    if (worktree / EXCLUSIONS).is_file():
        git.publication_paths.add(EXCLUSIONS)
    if not _acceptance_matches(n, ctx, git, approval):
        return "hash-mismatch"
    if not git.is_ancestor(base_sha, head):
        flag(ctx, n, Reason.RISK_HIGH, "PR base changed; retry implementation on the current base.", link)
        return "base-changed"

    if stopped := _verify_review_head(n, ctx, git, parsed, head, link):
        return stopped
    _check_publication(git, pr.base)

    if verdict_head is None and ctx.config.reviewer != "none":
        if runner._remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        if ctx.config.review_scope == "pr-body" and "## Review scope" not in pr.body:
            ctx.tracker.update_pr(pr.number, base=pr.base, title=pr.title,
                                  body=pr.body + "\n\n## Review scope\n\n" + review_scope_rules(ctx.config.acceptance_dir))
        review = _review(n, ctx, git, parsed, base_ref, round_number=_next_review_round(n, ctx),
                         pr=pr.number if ctx.config.review_scope == "pr-body" else None)
        ctx.tracker.comment(pr.number, f"Re-run review: VERDICT: {review.verdict}\n{review.raw}")
        if review.timed_out or runner._remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        if review.verdict != "PASS":
            flag(ctx, n, Reason.RETRIES_EXCEEDED, f"Re-run review {review.verdict}.", review.raw or review.findings)
            return "review-failed"

    from foragent.ci_mode import enabled, review_checks
    comparison_attempt = review_checks(n, ctx, git, pr.base) if enabled(ctx, pr.base) else None
    return _publish_reviewed_head(
        n, ctx, git, pr, head, base_ref, base_sha, approval, approved, batch_branch, parent,
        comparison_attempt=comparison_attempt,
    )
