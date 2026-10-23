"""Lane skills, review rounds, publication guards and review-only runs."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from itertools import islice
import json
from pathlib import Path
import re
import tempfile
import yaml
from foragent import board
from foragent.actions import FlagContext, flag, invalidate_approval
from foragent.config import resolved_reviewer
from foragent.git import GitError, GitRepo, PublicationError
from foragent.issue import ParsedIssue, body_hash, has_machine_check, parse_body
from foragent.labels import APPROVED, BLOCKED, READY, RUNNING
from foragent.orch_reinstall import after_merge
from foragent.policy import merge_decision, review_mode_for
from foragent.reasons import Reason
from foragent.skills import SkillManager, copy_lane_skills
from foragent.state import Approval
from foragent.tracker import TrackerError
from foragent.runner_core import AttemptFailure, RunContext, _append, _available_executor, _check, _ci_failure, _hold_changed_head, _merge_lock, _phase, _progress, _remaining, _test_slot, _timeout
from foragent.runner_prompts import ReviewResult, _claude_review_scope, _read_rebuttals, build_review_prompt, parse_code_review, parse_review, review_scope_rules


def _lane_notes(ctx: RunContext, n: int, worktree: Path) -> tuple[str | None, str]:
    lane = ctx.store.get_run(n).lane
    settings = ctx.config.lanes.get(lane, {}) if lane != "main" else {}
    agent = settings.get("agent")
    if not isinstance(agent, str) or not re.fullmatch(r"fa-[a-z0-9-]+", agent):
        return None, ""
    path = worktree / ".claude/agents" / f"{agent}.md"
    if not path.is_file():
        return None, ""
    body = path.read_text(encoding="utf-8", errors="replace")
    body = re.sub(r"\A---\s*\n.*?\n---[^\S\n]*(?:\n|$)", "", body, count=1, flags=re.DOTALL)
    memory = worktree / ".claude/agent-memory" / agent / "MEMORY.md"
    if memory.is_file():
        with memory.open(encoding="utf-8", errors="replace") as stream:
            body += "\n\nMemory:\n" + "".join(islice(stream, 200))
    return agent, "Lane notes:\n" + body.strip() + "\n\n"


def _lane_skill_names(ctx: RunContext, n: int, worktree: Path, agent: str | None) -> list[str]:
    lane = ctx.store.get_run(n).lane
    settings = ctx.config.lanes.get(lane, {}) if lane != "main" else {}
    skills = settings.get("skills", [])
    frontmatter = re.compile(r"\A---[^\S\n]*\n(.*?)^---[^\S\n]*(?:\n|$)", re.DOTALL | re.MULTILINE)
    if not skills and agent is not None:
        definition = (worktree / ".claude/agents" / f"{agent}.md").read_text(encoding="utf-8", errors="replace")
        match = frontmatter.match(definition)
        try:
            metadata = yaml.safe_load(match[1]) if match else None
        except yaml.YAMLError:
            metadata = None
        skills = metadata.get("skills", []) if isinstance(metadata, dict) else []
    return [name for name in skills if isinstance(name, str)] if isinstance(skills, list) else []


def _inject_lane_skills(ctx: RunContext, n: int, git: GitRepo, target_ref: str | None = None) -> None:
    target_ref = target_ref or f"origin/{ctx.config.base_branch}"
    git.local_skill_paths.update(ctx.store.get_run(n).local_skill_paths)
    git.reconcile_local_skills(target_ref)
    agent, _ = _lane_notes(ctx, n, git.root)
    names = _lane_skill_names(ctx, n, git.root, agent)
    names = [name for name in names if git.blob_sha(target_ref, f".claude/skills/{name}") is None]
    copied = copy_lane_skills(ctx.repo_root, git.root, names, SkillManager(ctx.config)) if names else []
    git.local_skill_paths.update(copied)
    ctx.store.update_run(n, local_skill_paths=sorted(git.local_skill_paths))


def _lane_skills(ctx: RunContext, n: int, worktree: Path, agent: str | None) -> tuple[str, list[str]]:
    """List installed skills so Codex can read them when relevant."""
    skills = _lane_skill_names(ctx, n, worktree, agent)
    frontmatter = re.compile(r"\A---[^\S\n]*\n(.*?)^---[^\S\n]*(?:\n|$)", re.DOTALL | re.MULTILINE)
    sections, missing = [], []
    for name in skills:
        path = worktree / ".claude/skills" / name / "SKILL.md"
        if not path.resolve().is_relative_to(worktree.resolve()) or not path.is_file():
            missing.append(name)
            continue
        match = frontmatter.match(path.read_text(encoding="utf-8", errors="replace"))
        try:
            metadata = yaml.safe_load(match[1]) if match else None
        except yaml.YAMLError:
            metadata = None
        description = metadata.get("description", "") if isinstance(metadata, dict) else ""
        description = " ".join(description.split()) if isinstance(description, str) else ""
        sections.append(f"- {name}: {description} (.claude/skills/{name}/SKILL.md)")
    return ("Lane skills (read the file when relevant and follow it):\n" + "\n".join(sections) + "\n\n"
            if sections else ""), missing


def _review(n: int, ctx: RunContext, git: GitRepo, parsed: ParsedIssue, base_ref: str,
            *, round_number: int = 1, pr: int | None = None) -> ReviewResult:
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
                ),
                model=ctx.config.reviewer_model, timeout_s=_remaining(n, ctx),
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
    _append(ctx.store.log_path(n), f"Review: VERDICT: {review.verdict}\n{review.findings}")
    _progress(n, ctx)
    return replace(review, raw=raw, timed_out=result.timed_out)


def _next_review_round(n: int, ctx: RunContext) -> int:
    rounds = [int(path.stem.removeprefix("review-"))
              for path in (ctx.store.root / "runs" / f"issue-{n}").glob("review-*.txt")
              if path.stem.removeprefix("review-").isdigit()]
    return max(rounds, default=0) + 1


def _batch_pr(parent: int, ctx: RunContext) -> None:
    children = ctx.tracker.sub_issues(parent)
    if not children or any(child.state != "closed" for child in children):
        return
    branch = f"foragent/batch-{parent}"
    if any(pr.head == branch for pr in ctx.tracker.list_prs("merged", branch)):
        return
    ctx.git.fetch("origin", ctx.config.base_branch)
    ctx.git.fetch("origin", branch)
    base = ctx.git.ref_sha(f"origin/{ctx.config.base_branch}")
    head = ctx.git.ref_sha(f"origin/{branch}")
    with tempfile.TemporaryDirectory(prefix=f"foragent-batch-{parent}-", dir=ctx.store.root) as directory:
        worktree = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, head)
        try:
            ctx.git.at(worktree).check_publication(base)
        finally:
            ctx.git.remove_worktree(worktree)
    prs = {pr.head: pr for pr in ctx.tracker.list_prs("merged", "foragent/issue-") if pr.base == branch}
    lines = [f"Closes #{parent}", "", "## Children", ""]
    for child in sorted(children, key=lambda child: child.number):
        pr = prs.get(f"foragent/issue-{child.number}")
        lines.append(f"- #{child.number}: {child.title}" + (f" (PR #{pr.number})" if pr else ""))
    title = f"Batch: {ctx.tracker.get_issue(parent).title}"
    body = "\n".join(lines)
    if (pr := ctx.tracker.find_pr(branch)) is not None:
        ctx.tracker.update_pr(pr, base=ctx.config.base_branch, title=title, body=body)
        board.sync_status(ctx, parent, "PR open")
        return
    pr = ctx.tracker.create_pr(head=branch, base=ctx.config.base_branch, title=title, body=body)
    board.sync_status(ctx, parent, "PR open")
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


def _check_publication(git: GitRepo, target_branch: str) -> None:
    try:
        git.check_publication(f"origin/{target_branch}")
    except PublicationError as exc:
        raise AttemptFailure(str(exc), target_branch) from exc


def _preserve_failed_implementation(
    n: int, ctx: FlagContext, git: GitRepo, reason: str, *, approval: Approval | None,
    approved_ref: str = "HEAD", title: str | None = None, pushed_head: str | None = None,
    tracker_available: bool = True, push_branch: Callable[[], None] | None = None,
) -> None:
    """Save stopped work before cleanup, including after the runner has died."""
    branch = f"foragent/issue-{n}"
    run = ctx.store.get_run(n)
    git.local_skill_paths.update(run.local_skill_paths if run is not None else ())
    conflicts = sorted(set(git.conflict_paths()) | set(run.merge_conflicts if run is not None else ()))
    if conflicts:
        ctx.store.update_run(n, merge_conflicts=conflicts)
        if git.merge_in_progress():
            # Save the exact partial resolution without declaring its target merged.
            git.quit_merge()
    git.stage_changes()
    if approval is not None:
        git.restore_staged_paths(approved_ref, approval.acceptance_blobs)
    if git.has_staged_changes():
        git.commit(f"WIP: {title or 'Issue'} (#{n}) [{reason}]")
    if git.head_sha() == pushed_head or not tracker_available:
        return
    try:
        pr = ctx.tracker.find_pr(branch)
    except TrackerError as exc:
        _append(ctx.store.log_path(n), f"WIP kept on local branch {branch}; open PR lookup failed: {exc}")
        return
    # An unverified WIP must not replace the head seen by an open PR's CI/review.
    if pr is None:
        if push_branch is not None:
            push_branch()
        else:
            git.push(branch)


def _review_only(n: int, ctx: RunContext, worktree: Path, verdict_head: str | None) -> str:
    """Verify one immutable PR head, then apply the ordinary merge policy."""
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
    if not _acceptance_matches(n, ctx, git, approval):
        return "hash-mismatch"
    if not git.is_ancestor(base_sha, head):
        flag(ctx, n, Reason.RISK_HIGH, "PR base changed; retry implementation on the current base.", link)
        return "base-changed"

    log = ctx.store.log_path(n)
    for command in parsed.done_when_commands:
        if _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        check = _check(command, worktree, log, min(900, _remaining(n, ctx)), progress=lambda: _progress(n, ctx),
                       slot=_test_slot(n, ctx))
        _progress(n, ctx)
        if check.timed_out or _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        if check.exit_code != 0:
            flag(ctx, n, Reason.RETRIES_EXCEEDED, "Done when failed on the PR head.",
                 f"Command: {command}\n{check.output}\n{link}")
            return "checks-failed"
    git.stage_changes()
    if git.head_sha() != head or git.has_staged_changes():
        flag(ctx, n, Reason.RETRIES_EXCEEDED, "Done when changed the verified PR contents.", link)
        return "checks-failed"
    _check_publication(git, pr.base)

    if verdict_head is None and ctx.config.reviewer != "none":
        if _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        if ctx.config.review_scope == "pr-body" and "## Review scope" not in pr.body:
            ctx.tracker.update_pr(pr.number, base=pr.base, title=pr.title,
                                  body=pr.body + "\n\n## Review scope\n\n" + review_scope_rules(ctx.config.acceptance_dir))
        review = _review(n, ctx, git, parsed, base_ref, round_number=_next_review_round(n, ctx),
                         pr=pr.number if ctx.config.review_scope == "pr-body" else None)
        ctx.tracker.comment(pr.number, f"Re-run review: VERDICT: {review.verdict}\n{review.raw}")
        if review.timed_out or _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        if review.verdict != "PASS":
            flag(ctx, n, Reason.RETRIES_EXCEEDED, f"Re-run review {review.verdict}.", review.raw or review.findings)
            return "review-failed"

    with _merge_lock(n, ctx):
        # A pass applies only to this head and these instructions. Neither the
        # reviewer nor an orchestrator verdict can waive the remaining checks.
        def unchanged() -> str | None:
            if not approved():
                return "hash-mismatch"
            if ctx.tracker.pr_head_sha(pr.number) != head:
                return _hold_changed_head(n, ctx, link)
            current = next((item for item in ctx.tracker.list_prs("open", pr.head)
                            if item.number == pr.number), None)
            ctx.git.fetch("origin", pr.base)
            if current is None or current.base != pr.base or ctx.git.ref_sha(base_ref) != base_sha:
                flag(ctx, n, Reason.RISK_HIGH, "PR base changed after verification; retry implementation.", link)
                return "base-changed"
            if not _acceptance_matches(n, ctx, git, approval):
                return "hash-mismatch"
            return None

        if stopped := unchanged():
            return stopped
        paths = sorted(set(git.changed_paths(base_ref)) | set(ctx.tracker.pr_changed_files(pr.number)))

        def policy_hold() -> str | None:
            decision = merge_decision(changed_paths=paths, issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config)
            if decision.kind == "hold":
                flag(ctx, n, decision.reason, decision.summary, link)
                return "held"
            if decision.kind == "hold_mode" and (decision.mode == "careful" or pr.base != batch_branch):
                flag(ctx, n, Reason.RISK_HIGH, "PR is waiting for your review.", link)
                return "pr-open"
            return None

        if stopped := policy_hold():
            return stopped
        has_workflows = any(path.is_file() for pattern in ("*.yml", "*.yaml")
                            for path in (worktree / ".github/workflows").glob(pattern))
        started = ctx.now().timestamp()
        for poll in range(41):
            if _remaining(n, ctx) <= 0:
                return _timeout(n, ctx)
            status = ctx.tracker.pr_checks(pr.number)
            _progress(n, ctx)
            if ctx.tracker.pr_head_sha(pr.number) != head:
                return _hold_changed_head(n, ctx, link)
            if status == "none" and has_workflows and ctx.now().timestamp() - started < 180:
                status = "pending"
            if status in {"pass", "none"}:
                break
            if status == "fail":
                _ci_failure(n, ctx, pr.number, head, pr.base)
            if ctx.now().timestamp() - started >= 1200 or poll == 40:
                flag(ctx, n, Reason.TIMEOUT, "CI did not finish", link)
                return "timeout"
            ctx.sleep(min(30, _remaining(n, ctx)))
        if stopped := unchanged():
            return stopped
        if stopped := policy_hold():
            return stopped
        if _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        _check_publication(git, pr.base)
        try:
            ctx.tracker.merge_pr(pr.number, method="squash", expected_head_sha=head)
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise
            if ctx.tracker.pr_head_sha(pr.number) != head:
                return _hold_changed_head(n, ctx, link)
            raise
        if review_mode_for(paths, ctx.config) == "fast":
            after_merge(ctx, pr.number, pr.base)
        try:
            if pr.base == batch_branch:
                ctx.tracker.close_issue(n, f"Merged into {batch_branch} (PR #{pr.number})")
            else:
                ctx.tracker.close_issue(n)
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise
            if ctx.tracker.current_issue(n).state != "closed":
                raise
        ctx.tracker.remove_labels(n, [READY, RUNNING, BLOCKED])
        if pr.base == batch_branch and parent is not None:
            try:
                _batch_pr(parent, ctx)
            except (GitError, TrackerError) as exc:
                if isinstance(exc, TrackerError) and exc.is_rate_limit:
                    raise
                _append(log, f"Batch PR finalization deferred to tick: {exc}")
        return "merged"
