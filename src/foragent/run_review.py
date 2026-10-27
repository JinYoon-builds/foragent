"""Independent review execution, findings, rebuttals and review-only verification."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
from foragent import orch_events, orch_reviewers, review_notes, review_perspectives, risks
from foragent.executors import ExecResult
from foragent.reasons import Reason
from foragent.run_attempt import WholeFailure
from foragent.run_checks import _acceptance_matches, _append, _verify_review_head
from foragent.run_attempt import ExecutorStopped
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
    conflicts: tuple[str, ...] = ()
    review_notes: tuple[str, ...] = ()  # reviewer NOTE lines: recorded, never findings


SPEC_CONFLICT = "SPEC-CONFLICT:"


def spec_conflicts(*texts: str) -> tuple[str, ...]:
    """Every distinct line, in order, that reports a conflict with a locked acceptance test."""
    lines = (line.strip() for text in texts for line in text.splitlines())
    return tuple(dict.fromkeys(line for line in lines if line.startswith(SPEC_CONFLICT)))


def _stop_on_spec_conflict(n: int, ctx: RunContext, review: ReviewResult) -> None:
    """A finding against a locked test is a question for the orchestrator, not a defect to retry."""
    if review.conflicts:
        flag(ctx, n, Reason.SPEC_QUESTION,
             "The reviewer found a conflict with a locked acceptance test.", "\n".join(review.conflicts))
        raise ExecutorStopped("spec-question")


def parse_review(message: str) -> ReviewResult:
    lines = message.splitlines()
    for index, line in enumerate(lines):
        if match := re.fullmatch(r"VERDICT: (PASS|FAIL)\s*", line):
            rest = [line for line in lines[index + 1:] if line.strip()]
            findings = [line for line in rest if review_notes.note_text(line) is None]
            notes = review_notes.extract(message)
            # A FAIL whose findings are all NOTE lines rejects nothing.
            verdict = "PASS" if match[1] == "FAIL" and notes and not findings else match[1]
            return ReviewResult(verdict, "\n".join(findings), review_notes=notes)
    return ReviewResult("UNUSABLE", "", ("reviewer gave no verdict",))


FINDING_LINE = re.compile(r"^(?:[-*]|\d+[.)])?\s*`?\S+?:\d+`?\s+(—|-|--)\s+")


def code_review_findings(text: str) -> tuple[str, list[str] | list[dict], str]:
    """/code-review result text as (verdict, findings, note).

    Findings are the path:line lines when there are any, else the finding objects of the
    one JSON payload (each keeps every field the reviewer wrote, such as ``class``).
    """
    # NOTE lines are plain text beside the findings; they never count as findings.
    text = review_notes.strip(text)
    lines = [line for line in text.splitlines() if line.strip()]
    findings = [line for line in lines if FINDING_LINE.match(line)]
    if findings:
        return "FAIL", findings, ""
    payload = text.strip()
    blocks = list(re.finditer(r"^[ \t]*```([^\r\n]*)\r?\n(.*?)^[ \t]*```[ \t]*\r?$", payload, re.M | re.S))
    json_blocks = [block for block in blocks if block[1].strip() == "json"]
    if len(json_blocks) > 1:
        return "UNUSABLE", [], "/code-review gave multiple findings payloads"
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
            return "PASS", [], ""
        if any(not isinstance(item, dict) or not {"file", "line", "summary"}.intersection(item) for item in items):
            return "UNUSABLE", [], "/code-review gave invalid findings"
        return "FAIL", items, ""
    if not blocks and lines and lines[0].strip() == "(none)":
        return "PASS", [], ""
    return "UNUSABLE", [], "/code-review gave no verdict"


def parse_code_review(message: str) -> ReviewResult:
    try:
        event = json.loads(message)
    except ValueError:
        event = None
    if not isinstance(event, dict) or event.get("is_error") or not isinstance(event.get("result"), str):
        return ReviewResult("UNUSABLE", "", ("/code-review gave no usable result",))
    verdict, findings, note = code_review_findings(event["result"])
    if verdict == "UNUSABLE":
        return ReviewResult("UNUSABLE", "", (note,))
    return ReviewResult(verdict, "\n".join(
        item if isinstance(item, str) else f"{item.get('file', '?')}:{item.get('line', '?')} — {item.get('summary', '')}"
        for item in findings
    ))


@contextmanager
def _claude_review_scope(git: GitRepo, body: str, acceptance_dir: str, rebuttals: str = "", risk_notes: str = ""):
    git.ignore_review_scope()
    path = git.root / REVIEW_SCOPE_FILE
    link = os.readlink(path) if path.is_symlink() else None
    original = path.read_bytes() if path.exists() else None
    mode = path.stat().st_mode if path.exists() else None
    section = (
        "# Review scope (written by Foragent for this review only)\n\n"
        f"{body}\n\n{review_scope_rules(acceptance_dir)}\n\n"
        + (f"{risk_notes}\n\n" if risk_notes else "")
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
    notes = _risk_notes(git, base_ref)
    if notes and len(shares := review_perspectives.shares(
            ctx.config, orch_reviewers.effective(ctx.store, ctx.config, n))) > 1:
        return _panel_review(n, ctx, git, parsed, base_ref, reviewer, rebuttals, notes, shares, round_number)
    risk_notes = review_perspectives.with_checklist(notes, ctx.config)
    settings = risks.review_options(ctx.config, reviewer, ctx.config.reviewer_model) if risk_notes else {
        "model": ctx.config.reviewer_model}
    edited = False
    with ExitStack() as scope:
        if reviewer == "claude" and ctx.config.review_scope == "claude-local":
            approval = ctx.store.load_approval(n)
            if approval is None:
                raise ValueError("Claude review requires an approved snapshot")
            scope.enter_context(_claude_review_scope(git, approval.body, ctx.config.acceptance_dir, rebuttals,
                                                      risk_notes))
        before = git.review_state()
        try:
            result = _available_executor(
                n, ctx, ctx.executor_factory(reviewer), worktree=git.root,
                reviewer=True,
                prompt="" if reviewer == "claude" else build_review_prompt(
                    parsed, git.staged_diff(base_ref), ctx.config.acceptance_dir, rebuttals=rebuttals,
                    full_text_dir=cuts.issue_dir(ctx.store.root, n),
                    risk_notes=risk_notes,
                ),
                **settings, timeout_s=runner._remaining(n, ctx),
                log_path=ctx.store.log_path(n), on_result=notes_of(n, ctx, reviewer, round_number), **options,
            )
            message = result.full_message if result.full_message is not None else result.last_message
            raw = _raw_text(reviewer, message)
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
        parser = parse_code_review if reviewer == "claude" else parse_review
        review = parser(message)
    if reviewer == "claude":
        review = ReviewResult(review.verdict, review.findings, (
            f"reviewer: /code-review {ctx.config.review_level}", *review.notes,
        ))
    if edited:
        review = ReviewResult(review.verdict, review.findings, (*review.notes, "Reviewer changes discarded; staged index restored."))
    return _finish_review(n, ctx, replace(review, raw=raw, timed_out=result.timed_out, conflicts=spec_conflicts(raw)),
                          round_number, (raw,))


def _finish_review(n: int, ctx: RunContext, review: ReviewResult, round_number: int,
                   texts: tuple[str, ...]) -> ReviewResult:
    """Log and journal one round; a round with spec conflicts is journaled as SPEC-CONFLICT.

    Every NOTE line in ``texts`` (each reviewer's raw text), whatever the verdict, is recorded
    once per issue as a review-note event.
    """
    review = replace(review, review_notes=review_notes.extract(*texts))
    _record_notes(n, ctx, round_number, *texts)
    if review.verdict == "PASS" and not review.conflicts:
        ctx.store.update_run(n, last_review_pass_at=ctx.now().isoformat())
    log = ctx.store.log_path(n)
    _append(log, f"Review: VERDICT: {review.verdict}\n{review.findings}"
            + "".join(f"\nNOTE: {note}" for note in review.review_notes))
    if review.conflicts:
        _append(log, "Review spec conflicts:\n" + "\n".join(review.conflicts))
        _journal_review(n, ctx, "SPEC-CONFLICT", "\n".join(review.conflicts), round_number,
                        review_verdict=review.verdict)
    else:
        _journal_review(n, ctx, review.verdict, review.findings, round_number)
    _progress(n, ctx)
    return review


def _raw_text(reviewer: str, message: str) -> str:
    """The reviewer's text: Claude's JSON result field when present, else the message itself."""
    if reviewer == "claude":
        try:
            event = json.loads(message)
        except ValueError:
            event = None
        if isinstance(event, dict) and isinstance(event.get("result"), str):
            return event["result"]
    return message


def _message(result: ExecResult) -> str:
    return result.full_message if result.full_message is not None else result.last_message


def _save_review(n: int, ctx: RunContext, name: str, raw: str) -> None:
    path = ctx.store.root / "runs" / f"issue-{n}" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")


def _panel_review(n: int, ctx: RunContext, git: GitRepo, parsed: ParsedIssue, base_ref: str, reviewer: str,
                  rebuttals: str, notes: str, shares: list[list[dict[str, str]]],
                  round_number: int) -> ReviewResult:
    """N independent reviewers run at once on disjoint perspective shares; one merge call decides.

    Every call uses the prompt path at the risk-map tier, whatever reviewer or review_scope is
    set. A usage limit in any reviewer stops the whole round (limit-wait) and it runs again in
    full; any other failure or timeout makes the round UNUSABLE, as a failed single review is.
    """
    from foragent import runner

    settings = risks.review_options(ctx.config, reviewer, ctx.config.reviewer_model)
    executor = ctx.executor_factory(reviewer)
    record = notes_of(n, ctx, reviewer, round_number)
    diff = git.staged_diff(base_ref)
    acceptance = ctx.config.acceptance_dir
    log = ctx.store.log_path(n)

    def call(prompt: str, log_path: Path = log) -> ExecResult:
        # The panel decides the round's outcome from every call, so no call blocks the issue itself.
        return _available_executor(n, ctx, executor, worktree=git.root, reviewer=True, prompt=prompt,
                                   **settings, timeout_s=runner._remaining(n, ctx), log_path=log_path,
                                   flag_exhaustion=False, on_result=record)

    def failure(label: str, error: BaseException) -> str:
        return f"{label} failed: {getattr(error, 'outcome', type(error).__name__)} {error}".rstrip()

    def unusable_error(label: str, error: BaseException) -> str:
        """Re-raise a usage limit or an interrupt; any other error becomes an UNUSABLE note."""
        if _is_limit(error) or not isinstance(error, Exception):
            raise error
        return failure(label, error)

    # Each concurrent reviewer logs to its own file: executors derive per-call output files
    # (Claude's `<log>.stdout`) from the log path, so a shared log would mix their outputs.
    logs = [log.with_name(f"{log.stem}-review-{round_number}-{k}.log") for k in range(1, len(shares) + 1)]

    def restore(before) -> bool:
        if git.review_state() == before:
            return False
        git.restore_review_state(before)
        _append(log, "Reviewer changed repository state; restored HEAD, index and working tree.")
        return True

    prompts = [build_review_prompt(parsed, diff, acceptance, rebuttals=rebuttals,
                                   full_text_dir=cuts.issue_dir(ctx.store.root, n),
                                   risk_notes=review_perspectives.with_checklist(notes, ctx.config, share))
               for share in shares]
    before = git.review_state()
    try:
        with ThreadPoolExecutor(max_workers=len(prompts), thread_name_prefix=f"review-{n}") as pool:
            futures = [pool.submit(call, prompt, path) for prompt, path in zip(prompts, logs)]
        errors = [future.exception() for future in futures]
    finally:
        edited = restore(before)
    count = len(futures)
    # A member's spec conflict stops the run whatever else happened in the round, so it is read
    # before any usage limit discards the round and it skips the merge call (which could drop it).
    member_conflicts = spec_conflicts(*(
        _raw_text(reviewer, _message(future.result())) for future, error in zip(futures, errors) if error is None))
    # Outcome order: an interrupt propagates first; then any usage limit wins over every other
    # reviewer outcome (the round is discarded and runs again in full); other errors are UNUSABLE.
    raised = [error for error in errors if error is not None]
    for error in ([error for error in raised if not isinstance(error, Exception)]
                  + ([] if member_conflicts else [error for error in raised if _is_limit(error)])):
        raise error
    problems: list[str] = []
    raws = []
    timed_out = False
    for k, (future, error) in enumerate(zip(futures, errors), start=1):
        if error is not None:
            problems.append(failure(f"Reviewer {k} of {count}", error))
            raws.append(problems[-1])
        else:
            result = future.result()
            message = result.full_message if result.full_message is not None else result.last_message
            raws.append(_raw_text(reviewer, message))
            timed_out |= result.timed_out
            if result.timed_out or result.exit_code != 0:
                problems.append(f"Reviewer {k} of {count} exit_code={result.exit_code}, timed_out={result.timed_out}")
            elif parse_review(raws[-1]).verdict == "UNUSABLE":
                # A reviewer without a VERDICT line gave no usable review; never let the merge cover for it.
                problems.append(f"Reviewer {k} of {count} gave no verdict")
        _save_review(n, ctx, f"review-{round_number}-{k}.txt", raws[-1])
    _append(log, "Parallel reviewer logs:\n" + "\n".join(str(path) for path in logs))
    findings = "\n\n".join(f"Reviewer {k}:\n{raw}" for k, raw in enumerate(raws, start=1))
    if problems or member_conflicts:
        raw = findings
        review = ReviewResult("UNUSABLE", "", tuple(problems) or ("Findings merge skipped: a reviewer reported a spec conflict",))
    else:
        try:
            result = call(build_review_prompt(parsed, diff, acceptance, rebuttals=rebuttals,
                                              full_text_dir=cuts.issue_dir(ctx.store.root, n),
                                              risk_notes=review_perspectives.with_checklist(notes, ctx.config),
                                              panel_findings=findings))
        except BaseException as error:
            result = None
            raw = unusable_error("Findings merge", error)
        finally:
            edited = restore(before) or edited
        if result is None:
            review = ReviewResult("UNUSABLE", "", (raw,))
        else:
            message = result.full_message if result.full_message is not None else result.last_message
            raw = _raw_text(reviewer, message)
            timed_out = result.timed_out
            review = (ReviewResult("UNUSABLE", "", (
                f"Findings merge exit_code={result.exit_code}, timed_out={result.timed_out}",
            )) if result.timed_out or result.exit_code != 0 else parse_review(raw))
    _save_review(n, ctx, f"review-{round_number}.txt", raw)
    review = ReviewResult(review.verdict, review.findings, (
        f"reviewers: {count} in parallel, one findings merge", *review.notes,
    ))
    if edited:
        review = ReviewResult(review.verdict, review.findings, (*review.notes, "Reviewer changes discarded; staged index restored."))
    # A member's conflict stops the round even when the merge call drops it.
    return _finish_review(n, ctx, replace(review, raw=raw, timed_out=timed_out,
                                          conflicts=spec_conflicts(*raws, raw)), round_number, (*raws, raw))


def notes_of(n: int, ctx: RunContext, reviewer: str, round_number: int):
    """An executor ``on_result`` hook: record each reviewer response's NOTE lines as it arrives.

    The executor wrapper discards a response on a usage limit or a transient retry; the hook
    sees it first, so a limit after the reviewer wrote its NOTE lines still keeps them.
    """
    return lambda result: _record_notes(n, ctx, round_number, _raw_text(reviewer, _message(result) or ""))


def _record_notes(n: int, ctx: RunContext, round_number: int, *texts: str) -> None:
    """Record a reviewer's NOTE lines as soon as its text exists, so no later exit can lose them."""
    review_notes.record(ctx.store, ctx.now(), n, review_notes.extract(*texts), round=round_number)


_FINDING_LOCATION = re.compile(r"[\w.-]*[/.][\w./-]*:\d+")


def _journal_review(n: int, ctx: RunContext, verdict: str, findings: str, round_number: int,
                    source: str = "reviewer", review_verdict: str | None = None) -> None:
    """Record the round's verdict locally before any next round can start.

    A SPEC-CONFLICT round also keeps the reviewer's own verdict (PASS, FAIL or UNUSABLE),
    so a reader can still count the defects found beside the conflict.
    """
    count = 0 if verdict == "UNUSABLE" else sum(
        1 for line in findings.splitlines() if _FINDING_LOCATION.search(line))
    orch_events.append_event(ctx.store, ctx.now(), n, "review", f"{verdict} {count} findings",
                             verdict=verdict, findings=count, round=round_number, source=source,
                             **({} if review_verdict is None else {"review_verdict": review_verdict}))


def _is_limit(error: BaseException) -> bool:
    return isinstance(error, ExecutorStopped) and error.outcome == "limit-wait"


def _risk_notes(git: GitRepo, base_ref: str) -> str:
    """Risk notes for the paths this head changes; empty when no mapped path changed."""
    changed = git.changed_paths(base_ref)
    return risks.review_notes(risks.matching(risks.load_at(git, base_ref), changed), changed)


def with_review_scope(body: str, acceptance_dir: str, language: str) -> str:
    """Add the review scope, or every rule line an older PR's existing scope section lacks."""
    rules = review_scope_rules(acceptance_dir, language)
    if "## Review scope" not in body:
        return body + "\n\n## Review scope\n\n" + rules
    missing = [line for line in rules.splitlines() if line not in body]
    if not missing:
        return body
    # An older section holding the remaining rules in order is replaced whole; otherwise
    # the missing lines (the spec-conflict rule, the defect-class question) lead the section.
    earlier = "\n".join(line for line in rules.splitlines() if line not in missing)
    if earlier and earlier in body:
        return body.replace(earlier, rules, 1)
    return body.replace("## Review scope\n\n", "## Review scope\n\n" + "\n".join(missing) + "\n", 1)


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
    from foragent.ci_mode import commit_exclusions
    from foragent.run_publish import _open_or_update_pr, _push_branch, _render_pr_body

    n, ctx, git = attempt.n, attempt.ctx, attempt.git
    worktree = attempt.worktree
    pr = None
    if ctx.config.review_scope == "pr-body":
        title = f"{ctx.tracker.get_issue(n).title} (#{n})"
        commit_exclusions(attempt, title)
        _push_branch(attempt)
        pr = _open_or_update_pr(attempt, title, _render_pr_body(attempt))
        _progress(n, ctx)
    review = _review(n, ctx, git, attempt.parsed, attempt.base_ref, round_number=_next_review_round(n, ctx), pr=pr)
    if pr is not None and review.conflicts:
        ctx.tracker.comment(pr, "\n".join([f"Round {len(attempt.reviews) + 1}: SPEC-CONFLICT", *review.conflicts]))
    _stop_on_spec_conflict(n, ctx, review)
    if review.verdict == "UNUSABLE":
        review = replace(review, findings=review_notes.strip(review.raw))
    attempt.reviews.append(review)
    if pr is not None:
        ctx.tracker.comment(pr, "\n".join([
            f"Round {len(attempt.reviews)}: VERDICT: {review.verdict}", review.findings,
            *(f"NOTE: {note}" for note in review.review_notes), *review.notes,
        ]))
    if review.verdict != "PASS":
        attempt.review_failures += 1
        attempt.review_feedback = True
        if review.verdict == "UNUSABLE":
            attempt.reviewer_text = review.findings
            attempt.failure = WholeFailure("A reviewer said (verbatim):\n" + review.findings)
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
        # The orchestrator's verdict for this exact head stands in for a reviewer round.
        _journal_review(n, ctx, "PASS", "", _next_review_round(n, ctx), source="orchestrator")

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
    if git.blob_sha("HEAD", EXCLUSIONS) is not None:
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
        if ctx.config.review_scope == "pr-body":
            # Native PR review reads only the PR body: refresh the risk notes for this head and base.
            body = with_review_scope(pr.body, ctx.config.acceptance_dir, ctx.config.language)
            body = risks.refresh_notes(body, review_perspectives.with_checklist(_risk_notes(git, base_ref), ctx.config))
            if body != pr.body:
                pr = replace(pr, body=body)
                ctx.tracker.update_pr(pr.number, base=pr.base, title=pr.title, body=pr.body)
        review = _review(n, ctx, git, parsed, base_ref, round_number=_next_review_round(n, ctx),
                         pr=pr.number if ctx.config.review_scope == "pr-body" else None)
        ctx.tracker.comment(pr.number, f"Re-run review: VERDICT: {review.verdict}\n{review.raw}")
        _stop_on_spec_conflict(n, ctx, review)
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
        # On its merge turn a moved base is merged in and this head's checks run again (#712).
        reverify=lambda new_head: _verify_review_head(n, ctx, git, parsed, new_head, link),
    )
