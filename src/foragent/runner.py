"""Execute, independently check and review one approved snapshot."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import traceback

from foragent.actions import flag, invalidate_approval
from foragent.config import Config, resolved_reviewer
from foragent.executors import ExecResult, Executor, executor_for
from foragent.git import GitError, GitRepo
from foragent.issue import ParsedIssue, has_machine_check, parse_body
from foragent.labels import READY, RUNNING
from foragent.policy import merge_decision, review_mode_for
from foragent.processes import run_logged
from foragent.reasons import Reason
from foragent.state import Approval, StateStore
from foragent.tracker import Tracker, TrackerError


def poll_executor(execute: Callable[[], ExecResult], poll: Callable[[], None]) -> ExecResult:
    """Keep execution on the main thread so subprocess termination still works."""
    stopped = threading.Event()

    def watch():
        while not stopped.wait(30):
            poll()

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        return execute()
    finally:
        stopped.set()
        watcher.join()


@dataclass
class RunContext:
    repo: str
    repo_root: Path
    tracker: Tracker
    store: StateStore
    config: Config
    git: GitRepo
    now: Callable[[], datetime]
    executor_factory: Callable[[str], Executor] = executor_for
    sleep: Callable[[float], None] = time.sleep
    poll_hook: Callable[[Callable[[], ExecResult], Callable[[], None]], ExecResult] = poll_executor


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int | None
    timed_out: bool
    output: str


REVIEW_PROMPT = (
    "Review this change for defects that break the stated goal, violate Out of scope, "
    "or would make the Done when commands pass without the goal being met "
    "(weakened checks, special-casing test inputs). Ignore style and nice-to-haves. "
    "Do not edit files. Reply with exactly one line `VERDICT: PASS` or `VERDICT: FAIL` "
    "followed by at most 5 findings, each one line with file:line and the concrete failure."
)


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    findings: str
    notes: tuple[str, ...] = ()


class PatchReplayError(GitError):
    """A child patch could not be replayed onto its selected destination."""


def build_review_prompt(parsed: ParsedIssue, diff: str) -> str:
    commands = "\n".join(f"```sh\n{command}\n```" for command in parsed.done_when_commands)
    return (
        f"{REVIEW_PROMPT}\n\nGoal:\n{parsed.goal}\n\nOut of scope:\n{parsed.out_of_scope}"
        f"\n\nDone when commands:\n{commands}\n\nDiff:\n{diff[:60000]}"
    )


def parse_review(message: str) -> ReviewResult:
    lines = message.splitlines()
    for index, line in enumerate(lines):
        if match := re.fullmatch(r"VERDICT: (PASS|FAIL)\s*", line):
            findings = [line for line in lines[index + 1:] if line.strip()][:5]
            return ReviewResult(match[1], "\n".join(findings))
    return ReviewResult("UNUSABLE", "", ("reviewer gave no verdict",))


def build_prompt(n: int, parsed: ParsedIssue, acceptance_dir: str, failure: str = "") -> str:
    commands = "\n".join(f"```sh\n{command}\n```" for command in parsed.done_when_commands)
    prompt = (
        f"You are implementing GitHub issue #{n} in this repository.\n"
        f"{parsed.goal}\n"
        f"Out of scope (do not do these): {parsed.out_of_scope}\n"
        "The work is accepted only when these commands succeed (they will be run for you afterwards):\n"
        f"{commands}\n"
        "Rules:\n"
        f"- Do not modify anything under {acceptance_dir}. Those tests are fixed.\n"
        "- If the issue needs a product decision you cannot make from the text and the code, do not guess:\n"
        "  write .foragent/QUESTION.md with the question, 2–3 options and your recommendation, then stop.\n"
        "- Do not commit, push or switch branches. Leave your changes in the working tree.\n"
    )
    if failure:
        prompt += "\nA previous attempt failed. Output of the failing check:\n" + failure[-4000:]
    return prompt


def _append(log: Path, text: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")


def _check(command: str, worktree: Path, log: Path, timeout_s: int) -> CheckResult:
    _append(log, f"\nDone when command:\n{command}")
    offset = log.stat().st_size
    code, timed_out, _ = run_logged(
        ["/bin/sh", "-c", command], cwd=worktree, timeout_s=timeout_s, log_path=log,
    )
    with log.open("rb") as stream:
        stream.seek(max(offset, log.stat().st_size - 16000))
        tail = stream.read().decode("utf-8", errors="replace")[-4000:]
    _append(log, f"\nCheck exit_code={code}, timed_out={timed_out}\nOutput tail:\n{tail}")
    return CheckResult(command, code, timed_out, tail)


def _pr_body(
    n: int, parsed: ParsedIssue, checks: list[CheckResult], attempt: int,
    reviews: list[ReviewResult],
) -> str:
    lines = [f"Closes #{n}", "", "| Done when command | Result |", "| --- | --- |"]
    for check in checks:
        command = check.command.replace("&", "&amp;").replace("<", "&lt;").replace("|", "&#124;")
        command = command.replace("`", "&#96;").replace("\n", "<br>")
        passed = check.exit_code == 0 and not check.timed_out
        lines.append(f"| <code>{command}</code> | {'pass' if passed else 'fail'} |")
    lines.extend(["", "## Review", ""])
    if not reviews:
        lines.append("Disabled (reviewer: none).")
    for round_number, review in enumerate(reviews, 1):
        lines.append(f"Round {round_number}: VERDICT: {review.verdict}")
        if review.findings:
            lines.append(review.findings)
        lines.extend(review.notes)
        lines.append("")
    if parsed.done_when_checklist:
        lines.extend(["", "## Needs a human check", ""])
        lines.extend(f"- [ ] {item.text}" for item in parsed.done_when_checklist)
    lines.extend(["", f"Opened by Foragent gate (attempt {attempt})"])
    return "\n".join(lines)


def _remaining(n: int, ctx: RunContext) -> int:
    run = ctx.store.get_run(n)
    if run is None:
        raise KeyError(n)
    elapsed = (ctx.now() - datetime.fromisoformat(run.started_at)).total_seconds()
    return max(0, int(ctx.config.max_runtime_minutes * 60 - elapsed))


def _timeout(n: int, ctx: RunContext) -> str:
    flag(ctx, n, Reason.TIMEOUT, "Too big for one session; split it in order")
    return "timeout"


def _hold_changed_head(n: int, ctx: RunContext, link: str) -> str:
    flag(ctx, n, Reason.RISK_HIGH, "PR head changed after verification", link)
    return "held"


def _progress(n: int, ctx: RunContext) -> None:
    ctx.store.update_run(n, last_progress_at=ctx.now().astimezone(UTC).isoformat())


def _run_executor(n: int, ctx: RunContext, executor: Executor, **kwargs) -> ExecResult:
    log = kwargs["log_path"]
    size = log.stat().st_size if log.exists() else 0

    def poll():
        nonlocal size
        current = log.stat().st_size if log.exists() else 0
        if current > size:
            _progress(n, ctx)
        size = current

    try:
        return ctx.poll_hook(lambda: executor.run(**kwargs), poll)
    finally:
        _progress(n, ctx)


def _review(n: int, ctx: RunContext, git: GitRepo, parsed: ParsedIssue, base_ref: str) -> ReviewResult:
    before = git.review_state()
    edited = False
    try:
        result = _run_executor(
            n, ctx, ctx.executor_factory(resolved_reviewer(ctx.config)), worktree=git.root,
            prompt=build_review_prompt(parsed, git.staged_diff(base_ref)),
            model=ctx.config.reviewer_model, timeout_s=_remaining(n, ctx),
            log_path=ctx.store.log_path(n),
        )
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
        review = parse_review(result.full_message if result.full_message is not None else result.last_message)
    if edited:
        review = ReviewResult(review.verdict, review.findings, (*review.notes, "Reviewer changes discarded; staged index restored."))
    _append(ctx.store.log_path(n), f"Review: VERDICT: {review.verdict}\n{review.findings}")
    _progress(n, ctx)
    return review


def _batch_pr(parent: int, ctx: RunContext) -> None:
    children = ctx.tracker.sub_issues(parent)
    if not children or any(child.state != "closed" for child in children):
        return
    branch = f"foragent/batch-{parent}"
    if any(pr.head == branch for pr in ctx.tracker.list_prs("merged", branch)):
        return
    prs = {pr.head: pr for pr in ctx.tracker.list_prs("merged", "foragent/issue-") if pr.base == branch}
    lines = [f"Closes #{parent}", "", "## Children", ""]
    for child in sorted(children, key=lambda child: child.number):
        pr = prs.get(f"foragent/issue-{child.number}")
        lines.append(f"- #{child.number}: {child.title}" + (f" (PR #{pr.number})" if pr else ""))
    title = f"Batch: {ctx.tracker.get_issue(parent).title}"
    body = "\n".join(lines)
    if (pr := ctx.tracker.find_pr(branch)) is not None:
        ctx.tracker.update_pr(pr, base=ctx.config.base_branch, title=title, body=body)
        return
    pr = ctx.tracker.create_pr(head=branch, base=ctx.config.base_branch, title=title, body=body)
    ctx.tracker.comment(parent, f"PR #{pr} is waiting for your review\n\nhttps://github.com/{ctx.repo}/pull/{pr}")


def _acceptance_matches(
    n: int, ctx: RunContext, git: GitRepo, approval: Approval, ref: str = "HEAD",
) -> bool:
    blobs = git.blob_shas(ref, approval.acceptance_blobs)
    mismatches = [path for path, sha in approval.acceptance_blobs.items() if blobs.get(path) != sha]
    if mismatches:
        invalidate_approval(
            ctx, n, "Approval invalidated: worktree acceptance files changed. Re-approve to run it.",
            "\n".join(f"Acceptance file changed: `{path}`." for path in mismatches),
        )
    return not mismatches


def _execute(n: int, ctx: RunContext, worktree: Path) -> str:
    approval = ctx.store.load_approval(n)
    if approval is None:
        ctx.tracker.remove_labels(n, [RUNNING])
        return "no-approval"

    branch = f"foragent/issue-{n}"
    base_ref = f"origin/{ctx.config.base_branch}"
    parent = ctx.tracker.parent_of(n) if "batch" in {ctx.config.review, *ctx.config.review_paths.values()} else None
    batch_branch = f"foragent/batch-{parent}" if parent is not None else None
    target_branch = ctx.config.base_branch
    ctx.git.remove_worktree(worktree)
    ctx.git.fetch("origin", ctx.config.base_branch)
    if not _acceptance_matches(n, ctx, ctx.git, approval, base_ref):
        return "hash-mismatch"
    if ctx.config.review == "batch" and batch_branch:
        ctx.git.ensure_batch_branch(batch_branch, base_ref)
        target_branch = batch_branch
    start_ref = f"origin/{target_branch}"
    ctx.git.add_worktree(worktree, branch, start_ref)
    git = ctx.git.at(worktree)
    if not _acceptance_matches(n, ctx, git, approval):
        return "hash-mismatch"
    _progress(n, ctx)

    parsed = parse_body(approval.body)
    if not has_machine_check(parsed):
        flag(ctx, n, Reason.NO_MACHINE_CHECK, "Done when has no machine check.")
        return "no-machine-check"
    executor = ctx.executor_factory(ctx.config.executor)
    log = ctx.store.log_path(n)
    failure = ""
    reviews: list[ReviewResult] = []
    review_failures = 0
    review_hold = False
    unusable_review = False

    def prepare_target() -> bool:
        nonlocal start_ref, target_branch
        if not batch_branch:
            return True
        paths = git.changed_paths(start_ref)
        mode = review_mode_for(paths, ctx.config)
        destination = batch_branch if mode == "batch" else ctx.config.base_branch if mode == "fast" else target_branch
        if destination == target_branch:
            return True
        # Path overrides can select batch after implementation or generated
        # files, or return to fast after a retry removes those paths.
        if destination == batch_branch:
            ctx.git.ensure_batch_branch(batch_branch, base_ref)
        with tempfile.NamedTemporaryFile() as patch:
            git.write_staged_patch(start_ref, Path(patch.name))
            ctx.git.remove_worktree(worktree)
            start_ref = f"origin/{destination}"
            target_branch = destination
            ctx.git.add_worktree(worktree, branch, start_ref)
            if not _acceptance_matches(n, ctx, git, approval):
                return False
            try:
                git.apply_staged_patch(Path(patch.name))
            except GitError as exc:
                raise PatchReplayError(str(exc)) from exc
        return True

    while True:
        remaining = _remaining(n, ctx)
        if remaining <= 0:
            return _timeout(n, ctx)
        result = _run_executor(
            n, ctx, executor,
            worktree=worktree, prompt=build_prompt(n, parsed, ctx.config.acceptance_dir, failure),
            model=ctx.config.executor_model, timeout_s=remaining, log_path=log,
        )
        run = ctx.store.get_run(n)
        if result.timed_out or _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        question = worktree / ".foragent/QUESTION.md"
        if question.exists():
            with question.open(encoding="utf-8", errors="replace") as stream:
                details = stream.read(6000)
            flag(ctx, n, Reason.SPEC_QUESTION, "The executor needs a product decision.", details)
            return "spec-question"

        try:
            git.stage_changes()
            if not prepare_target():
                return "hash-mismatch"
            paths = git.changed_paths(start_ref)
            _progress(n, ctx)
            checks = []
            if not paths:
                failure = "The session made no changes."
            else:
                staged_tree = git.staged_tree_sha()
                for verification in range(2):
                    checks = []
                    for command in parsed.done_when_commands:
                        checks.append(_check(command, worktree, log, 900))
                        _progress(n, ctx)
                    git.stage_changes()
                    if not prepare_target():
                        return "hash-mismatch"
                    paths = git.changed_paths(start_ref)
                    checked_tree = git.staged_tree_sha()
                    if checked_tree != staged_tree:
                        if verification == 1:
                            failure = "Done when commands modify tracked files"
                            break
                        _append(log, "Done when changed staged contents; checking the final contents again.")
                        staged_tree = checked_tree
                        continue
                    failures = [check for check in checks if check.exit_code != 0 or check.timed_out]
                    failure = (
                        f"Command: {failures[-1].command}\n{failures[-1].output}"
                        + ("\nCheck timed out." if failures[-1].timed_out else "")
                        if failures else ""
                    )
                    if not paths and not failure:
                        failure = "The session made no changes."
                    break
        except PatchReplayError as exc:
            failure = f"Could not apply child changes to {target_branch}: {exc}"
            # A three-way failure can leave unmerged index entries. Start the
            # next implementation attempt from the destination's clean tree.
            ctx.git.remove_worktree(worktree)
            ctx.git.add_worktree(worktree, branch, start_ref)
            paths = []
        if not failure and ctx.config.reviewer != "none":
            if _remaining(n, ctx) <= 0:
                return _timeout(n, ctx)
            review = _review(n, ctx, git, parsed, base_ref)
            reviews.append(review)
            if review.verdict == "UNUSABLE":
                review = _review(n, ctx, git, parsed, base_ref)
                reviews.append(review)
                if review.verdict != "PASS":
                    failure = review.findings or "\n".join(review.notes)
                    review_hold = unusable_review = True
                    break
            if review.verdict == "FAIL":
                review_failures += 1
                failure = review.findings or "Reviewer found defects (no findings provided)."
                if review_failures > ctx.config.review_max:
                    review_hold = True
                    break
        if not failure:
            break
        _append(log, f"Attempt {run.attempts} failed:\n{failure}")
        if run.attempts < ctx.config.max_retries + 1:
            ctx.store.update_run(n, attempts=run.attempts + 1)
            continue
        flag(ctx, n, Reason.RETRIES_EXCEEDED, "Checks or review failed after all attempts.", failure)
        # Preserve the failed implementation, rather than pushing an unchanged base.
        if paths:
            title = ctx.tracker.get_issue(n).title
            git.commit(f"{title} (#{n})")
        git.push(branch)
        return "retries-exceeded"

    # Only metadata is read live; instructions and checks always use the snapshot.
    issue = ctx.tracker.get_issue(n)
    title = f"{issue.title} (#{n})"
    git.commit(title)
    _progress(n, ctx)
    verified_head_sha = git.head_sha()
    git.push(branch)
    _progress(n, ctx)
    _append(log, f"Pushed verified HEAD: {verified_head_sha}")
    pr = ctx.tracker.find_pr(branch)
    body = _pr_body(n, parsed, checks, run.attempts, reviews)
    if pr is None:
        pr = ctx.tracker.create_pr(
            head=branch, base=target_branch, title=title,
            body=body,
        )
    else:
        ctx.tracker.update_pr(pr, base=target_branch, title=title, body=body)
    _progress(n, ctx)
    pr_ready_at = ctx.now().timestamp()
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
        return _hold_changed_head(n, ctx, link)
    if review_hold:
        summary = ("Reviewer did not return a usable verdict" if unusable_review
                   else f"Reviewer still found defects after {review_failures} rounds")
        flag(ctx, n, Reason.RISK_HIGH, summary, f"{failure}\n\n{link}")
        return "held"
    decision = merge_decision(
        changed_paths=ctx.tracker.pr_changed_files(pr),
        issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
    )
    if decision.kind == "hold":
        flag(ctx, n, decision.reason, decision.summary, link)
        return "held"
    if decision.kind == "hold_mode" and (decision.mode == "careful" or not batch_branch):
        ctx.tracker.comment(n, f"PR #{pr} is waiting for your review\n\n{link}")
        ctx.tracker.remove_labels(n, [RUNNING])
        return "pr-open"

    # A bounded iteration count also works with a frozen test clock.
    has_workflows = any(
        path.is_file()
        for pattern in ("*.yml", "*.yaml")
        for path in (worktree / ".github/workflows").glob(pattern)
    )
    deadline = ctx.now().timestamp() + 20 * 60
    for poll in range(41):
        status = ctx.tracker.pr_checks(pr)
        _progress(n, ctx)
        if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
            return _hold_changed_head(n, ctx, link)
        # Actions checks may not be registered immediately after opening the PR.
        if status == "none" and has_workflows and ctx.now().timestamp() - pr_ready_at < 180:
            status = "pending"
        if status in {"pass", "none"}:
            try:
                ctx.tracker.merge_pr(pr, method="squash", expected_head_sha=verified_head_sha)
            except TrackerError:
                # The atomic merge guard also covers a push after the last poll.
                if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                    return _hold_changed_head(n, ctx, link)
                raise
            _progress(n, ctx)
            try:
                if target_branch == batch_branch:
                    ctx.tracker.close_issue(n, f"Merged into {batch_branch} (PR #{pr})")
                else:
                    ctx.tracker.close_issue(n)
            except TrackerError:
                if ctx.tracker.get_issue(n).state != "closed":
                    raise
            ctx.tracker.remove_labels(n, [READY, RUNNING])
            if target_branch == batch_branch and parent is not None:
                try:
                    _batch_pr(parent, ctx)
                except TrackerError as exc:
                    _append(log, f"Batch PR finalization deferred to tick: {exc}")
            _progress(n, ctx)
            return "merged"
        if status == "fail":
            flag(ctx, n, Reason.RETRIES_EXCEEDED, "CI failed", link)
            return "ci-failed"
        remaining = deadline - ctx.now().timestamp()
        if remaining <= 0 or poll == 40:
            break
        ctx.sleep(min(30, remaining))
    flag(ctx, n, Reason.TIMEOUT, "CI did not finish", link)
    return "timeout"


def run_issue(n: int, ctx: RunContext) -> str:
    outcome = "error"
    worktree = ctx.store.worktree_path(n)
    log = ctx.store.log_path(n)
    try:
        ctx.store.update_run(n, pid=os.getpid())
        try:
            outcome = _execute(n, ctx, worktree)
        finally:
            ctx.git.remove_worktree(worktree)
    except Exception as exc:
        _append(log, traceback.format_exc())
        flag(ctx, n, Reason.RETRIES_EXCEEDED, f"Runner failed: {' '.join(str(exc).split())}")
        outcome = "error"
    except SystemExit:
        outcome = "killed"
        raise
    finally:
        # A tick may already have finished the record while terminating this runner.
        run = ctx.store.get_run(n)
        if run is not None and run.status != "finished":
            ctx.store.finish_run(n, outcome=run.outcome or outcome)
    return outcome
