"""One reviewer call, with either a full review or a saved-class confirmation task."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from foragent import cuts, orch_events, risks
from foragent.git import GitRepo
from foragent.issue import ParsedIssue
from foragent.run_prompts import build_confirm_prompt, build_review_prompt

if TYPE_CHECKING:
    from foragent.runner import RunContext
    from foragent.run_review import ReviewResult
    from foragent.state import StateStore


def first_review_findings(store: StateStore, n: int, round_number: int) -> str:
    """Read the earliest saved round at call time, excluding individual panel members.

    A missing or finding-free first review leaves the ordinary review path in charge.
    Native Claude reviews have no VERDICT line, so recognize their existing findings format too.
    """
    from foragent.run_review import code_review_findings, parse_review

    if round_number <= 1:
        return ""
    rounds = [(int(path.stem.removeprefix("review-")), path)
              for path in (store.root / "runs" / f"issue-{n}").glob("review-*.txt")
              if path.stem.removeprefix("review-").isdigit()]
    if not rounds:
        return ""
    number, path = min(rounds)
    if number >= round_number:
        return ""
    # Failed calls and incomplete panels also save raw findings; they are not an authoritative review.
    if (orch_events.has_event(store, n, "review", round=number, verdict="UNUSABLE")
            or orch_events.has_event(store, n, "review", round=number, review_verdict="UNUSABLE")):
        return ""
    raw = path.read_text(encoding="utf-8", errors="replace")
    review = parse_review(raw)
    if review.verdict == "UNUSABLE":
        verdict, findings, _ = code_review_findings(raw)
        return raw if verdict == "FAIL" and findings else ""
    return raw if review.verdict == "FAIL" and review.findings.strip() else ""


def single_review(n: int, ctx: RunContext, git: GitRepo, parsed: ParsedIssue, base_ref: str,
                  reviewer: str, rebuttals: str, decisions: str, *, round_number: int,
                  pr: int | None = None, first_review: str = "") -> ReviewResult:
    from foragent import runner
    from foragent.run_review import (
        ReviewResult, _append, _available_executor, _claude_review_scope, _finish_review,
        _raw_text, _save_review, notes_of, parse_code_review, parse_review, spec_conflicts,
    )

    native = reviewer == "claude" and not first_review
    options = {"review_level": ctx.config.review_level} if native else {}
    if native and pr is not None:
        options["review_pr"] = pr
    settings = (risks.review_options(ctx.config, reviewer, ctx.config.reviewer_model) if first_review
                else {"model": ctx.config.reviewer_model})
    prompt = ""
    if not native:
        context = dict(rebuttals=rebuttals, full_text_dir=cuts.issue_dir(ctx.store.root, n),
                       lane_decisions=decisions)
        diff = git.staged_diff(base_ref)
        prompt = (build_confirm_prompt(parsed, diff, ctx.config.acceptance_dir, first_review=first_review, **context)
                  if first_review else build_review_prompt(parsed, diff, ctx.config.acceptance_dir, **context))
    edited = False
    with ExitStack() as scope:
        if native and ctx.config.review_scope == "claude-local":
            approval = ctx.store.load_approval(n)
            if approval is None:
                raise ValueError("Claude review requires an approved snapshot")
            scope.enter_context(_claude_review_scope(git, approval.body, ctx.config.acceptance_dir, rebuttals,
                                                      lane_decisions=decisions))
        before = git.review_state()
        try:
            result = _available_executor(
                n, ctx, ctx.executor_factory(reviewer), worktree=git.root, reviewer=True,
                prompt=prompt,
                **settings, timeout_s=runner._remaining(n, ctx),
                log_path=ctx.store.log_path(n), on_result=notes_of(n, ctx, reviewer, round_number), **options,
            )
            message = result.full_message if result.full_message is not None else result.last_message
            raw = _raw_text(reviewer, message) if native else message
            _save_review(n, ctx, f"review-{round_number}.txt", raw)
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
        parser = parse_code_review if native else parse_review
        review = parser(message)
    if native:
        review = replace(review, notes=(f"reviewer: /code-review {ctx.config.review_level}", *review.notes))
    if edited:
        review = replace(review, notes=(*review.notes, "Reviewer changes discarded; staged index restored."))
    return _finish_review(n, ctx, replace(review, raw=raw, timed_out=result.timed_out, conflicts=spec_conflicts(raw)),
                          round_number, (raw,))
