"""Execute one approved snapshot and apply the fast review policy."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import time
import traceback

from foragent.actions import flag, invalidate_approval
from foragent.config import Config
from foragent.executors import Executor, executor_for
from foragent.git import GitRepo
from foragent.issue import ParsedIssue, has_machine_check, parse_body
from foragent.labels import READY, RUNNING
from foragent.policy import merge_decision
from foragent.processes import run_logged
from foragent.reasons import Reason
from foragent.state import StateStore
from foragent.tracker import Tracker, TrackerError


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


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int | None
    timed_out: bool
    output: str


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


def _pr_body(n: int, parsed: ParsedIssue, checks: list[CheckResult], attempt: int) -> str:
    lines = [f"Closes #{n}", "", "| Done when command | Result |", "| --- | --- |"]
    for check in checks:
        command = check.command.replace("&", "&amp;").replace("<", "&lt;").replace("|", "&#124;")
        command = command.replace("`", "&#96;").replace("\n", "<br>")
        passed = check.exit_code == 0 and not check.timed_out
        lines.append(f"| <code>{command}</code> | {'pass' if passed else 'fail'} |")
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


def _execute(n: int, ctx: RunContext, worktree: Path) -> str:
    approval = ctx.store.load_approval(n)
    if approval is None:
        ctx.tracker.remove_labels(n, [RUNNING])
        return "no-approval"

    branch = f"foragent/issue-{n}"
    base_ref = f"origin/{ctx.config.base_branch}"
    ctx.git.remove_worktree(worktree)
    ctx.git.fetch("origin", ctx.config.base_branch)
    ctx.git.add_worktree(worktree, branch, base_ref)
    git = ctx.git.at(worktree)
    blobs = git.blob_shas("HEAD", approval.acceptance_blobs)
    mismatches = [path for path, sha in approval.acceptance_blobs.items() if blobs.get(path) != sha]
    if mismatches:
        invalidate_approval(
            ctx, n, "Approval invalidated: worktree acceptance files changed. Re-approve to run it.",
            "\n".join(f"Acceptance file changed: `{path}`." for path in mismatches),
        )
        return "hash-mismatch"

    parsed = parse_body(approval.body)
    if not has_machine_check(parsed):
        flag(ctx, n, Reason.NO_MACHINE_CHECK, "Done when has no machine check.")
        return "no-machine-check"
    executor = ctx.executor_factory(ctx.config.executor)
    log = ctx.store.log_path(n)
    failure = ""
    while True:
        remaining = _remaining(n, ctx)
        if remaining <= 0:
            return _timeout(n, ctx)
        result = executor.run(
            worktree=worktree, prompt=build_prompt(n, parsed, ctx.config.acceptance_dir, failure),
            model=ctx.config.executor_model, timeout_s=remaining, log_path=log,
        )
        run = ctx.store.update_run(n, last_progress_at=ctx.now().astimezone(UTC).isoformat())
        if result.timed_out or _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        question = worktree / ".foragent/QUESTION.md"
        if question.exists():
            with question.open(encoding="utf-8", errors="replace") as stream:
                details = stream.read(6000)
            flag(ctx, n, Reason.SPEC_QUESTION, "The executor needs a product decision.", details)
            return "spec-question"

        git.stage_changes()
        paths = git.changed_paths(base_ref)
        checks = []
        if not paths:
            failure = "The session made no changes."
        else:
            staged_tree = git.staged_tree_sha()
            for verification in range(2):
                checks = [_check(command, worktree, log, 900) for command in parsed.done_when_commands]
                git.stage_changes()
                paths = git.changed_paths(base_ref)
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
        if not failure:
            break
        _append(log, f"Attempt {run.attempts} failed:\n{failure}")
        if run.attempts < ctx.config.max_retries + 1:
            ctx.store.update_run(n, attempts=run.attempts + 1)
            continue
        flag(ctx, n, Reason.RETRIES_EXCEEDED, "Done when checks failed after all attempts.", failure)
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
    verified_head_sha = git.head_sha()
    git.push(branch)
    _append(log, f"Pushed verified HEAD: {verified_head_sha}")
    pr = ctx.tracker.find_pr(branch)
    if pr is None:
        pr = ctx.tracker.create_pr(
            head=branch, base=ctx.config.base_branch, title=title,
            body=_pr_body(n, parsed, checks, run.attempts),
        )
    pr_ready_at = ctx.now().timestamp()
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
        return _hold_changed_head(n, ctx, link)
    decision = merge_decision(
        changed_paths=ctx.tracker.pr_changed_files(pr),
        issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
    )
    if decision.kind == "hold":
        flag(ctx, n, decision.reason, decision.summary, link)
        return "held"
    if decision.kind == "hold_mode":
        ctx.tracker.comment(n, f"{decision.summary}\n\n{link}")
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
            try:
                ctx.tracker.close_issue(n)
            except TrackerError:
                if ctx.tracker.get_issue(n).state != "closed":
                    raise
            ctx.tracker.remove_labels(n, [READY, RUNNING])
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
            ctx.store.finish_run(n, outcome=outcome)
    return outcome
