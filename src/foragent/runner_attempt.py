"""Implementation, verification and conflict steps of one runner attempt."""

from pathlib import Path
import shlex
import tempfile
from foragent import board
from foragent.actions import flag
from foragent.executors import classify_result
from foragent.git import GitError, GitRepo
from foragent.issue import parse_body
from foragent.labels import attempt_tier, issue_size
from foragent.order import detect_test_commands
from foragent.policy import review_mode_for, touched
from foragent.reasons import Reason
from foragent.runner_core import AttemptFailure, CIFailure, CheckResult, ConflictBudgetExhausted, ExecutorStopped, PatchReplayError, RunContext, _append, _available_executor, _check, _phase, _progress, _remaining, _test_slot, _timeout
from foragent.runner_prompts import ReviewResult, _pr_body, _read_rebuttals, build_prompt, build_resume_prompt, goal_condition
from foragent.runner_checks import _capture_acceptance_baseline, _pytest_command, _related_unit_tests, _write_claude_settings
from foragent.runner_review import _acceptance_matches, _check_publication, _inject_lane_skills, _lane_notes, _lane_skills


class _Attempt:
    """Shared state and steps of one implementation/check/review/publish loop."""

    def __init__(self, n: int, ctx: RunContext, git: GitRepo, target_branch: str, failure: str | CIFailure):
        self.n = n
        self.ctx = ctx
        self.git = git
        self.target_branch = target_branch
        self.failure = failure
        self.approval = ctx.store.load_approval(n)
        self.parsed = parse_body(self.approval.body)
        self.worktree = worktree = git.root
        self.branch = f"foragent/issue-{n}"
        self.base_ref = f"origin/{ctx.config.base_branch}"
        self.parent = parent = (
            ctx.tracker.parent_of(n) if "batch" in {ctx.config.review, *ctx.config.review_paths.values()} else None
        )
        self.batch_branch = f"foragent/batch-{parent}" if parent is not None else None
        self.start_ref = f"origin/{target_branch}"
        self.executor = ctx.executor_factory(ctx.config.executor)
        self.native_claude = native_claude = ctx.config.executor == "claude"
        self.options = {
            "settings_path": _write_claude_settings(n, ctx, worktree),
            "goal": goal_condition(ctx.config.acceptance_dir),
        } if native_claude else {}
        self.log = ctx.store.log_path(n)
        self.reviews: list[ReviewResult] = []
        self.review_failures = 0
        self.review_hold = False
        self.review_hold_reason = Reason.RISK_HIGH
        self.rebuttals: list[str] = []
        self.checks: list[CheckResult] = []
        self.paths: list[str] = []
        self.missing_skills: list[str] = []
        self.conflict_attempts = 0
        self.rebuilt = False
        self.rebuild_lease: str | None = None
        self.pushed_head: str | None = None
        self.issue = ctx.tracker.get_issue(n)
        self.size = issue_size(self.issue.labels)
        baseline_test_command = ctx.config.test_command
        if baseline_test_command is None:
            baseline_test_command = next(iter(detect_test_commands(worktree)), None)
        self.baseline = _capture_acceptance_baseline(n, ctx, baseline_test_command, self.start_ref)

    def implement(
        self, failure: str | CIFailure, *, merge_conflicts: list[str] | None = None, reviewer_text: str | None = None,
    ) -> None:
        n, ctx, worktree, options, native_claude = self.n, self.ctx, self.worktree, self.options, self.native_claude
        _phase(n, ctx, "resolving-conflict" if merge_conflicts is not None else "implementing")
        remaining = _remaining(n, ctx)
        if remaining <= 0:
            raise ExecutorStopped(_timeout(n, ctx))
        run = ctx.store.get_run(n)
        tier = attempt_tier(self.size, run.attempts)
        ctx.store.update_run(n, tier=tier, attempt_tiers={**run.attempt_tiers, str(run.attempts): tier})
        settings = ctx.config.tiers[tier]
        lane = ctx.config.lanes.get(run.lane, {})
        model = ctx.config.executor_model
        if native_claude:
            model = lane.get("model") or settings["claude_model"] or model
        else:
            options["effort"] = settings["codex_effort"]
        agent, notes = _lane_notes(ctx, n, worktree)
        if native_claude and agent is not None:
            options["agent"] = agent
        if not native_claude:
            skill_notes, missing = _lane_skills(ctx, n, worktree, agent)
            notes += skill_notes
            self.missing_skills.extend(name for name in missing if name not in self.missing_skills)
        hint_path = ctx.store.root / "hints" / f"{n}.txt"
        hint = hint_path.read_text(encoding="utf-8") if hint_path.is_file() else None
        failure_context = str(failure) or run.resume_reason
        test_command = ctx.config.test_command
        if test_command is None:
            test_command = next(iter(detect_test_commands(worktree)), None)
        prompt = build_prompt(
            n, self.parsed, ctx.config.acceptance_dir, failure_context, ctx.config.language,
            native_goal=not native_claude and ctx.config.codex_goal, acceptance_locked=native_claude,
            merge_conflicts=merge_conflicts,
            reviewer_text=reviewer_text,
            orchestrator_hint=hint,
            test_command=test_command,
            acceptance_baseline=self.baseline.node_ids if self.baseline else (),
            failure_limit=None if isinstance(failure, CIFailure) else 4000,
        )
        result = _available_executor(
            n, ctx, self.executor, worktree=worktree,
            prompt=prompt if native_claude else notes + prompt,
            session_body=self.approval.body,
            resume_prompt=build_resume_prompt(
                n, run.session_body, self.approval.body, failure_context,
                reviewer_text=reviewer_text, orchestrator_hint=hint, merge_conflicts=merge_conflicts,
            ),
            model=model, timeout_s=remaining, log_path=self.log, **options,
        )
        # A prepared prompt has not been delivered yet. Keep its hint through
        # availability waits, launch failures and interruptions; acknowledge it
        # only once the implementer returns an ordinary execution result.
        if hint is not None and classify_result(result) == "normal":
            hint_path.unlink()
        rebuttal = _read_rebuttals(worktree)
        if rebuttal.strip() and rebuttal not in self.rebuttals:
            self.rebuttals.append(rebuttal)
        if result.timed_out or _remaining(n, ctx) <= 0:
            raise ExecutorStopped(_timeout(n, ctx))
        question = worktree / ".foragent/QUESTION.md"
        if question.exists():
            with question.open(encoding="utf-8", errors="replace") as stream:
                details = stream.read(6000)
            flag(ctx, n, Reason.SPEC_QUESTION, "The executor needs a product decision.", details)
            raise ExecutorStopped("spec-question")

    def recreate_worktree(self) -> None:
        ctx, worktree = self.ctx, self.worktree
        artifacts = {name: path.read_bytes() for name in ("TRY.md", "REBUTTAL.md")
                     if (path := worktree / ".foragent" / name).is_file()}
        ctx.git.remove_worktree(worktree)
        ctx.git.add_worktree(worktree, self.branch, self.start_ref)
        ctx.store.update_run(self.n, merge_conflicts=[])
        _inject_lane_skills(ctx, self.n, self.git, f"origin/{self.target_branch}")
        for name, content in artifacts.items():
            path = worktree / ".foragent" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def prepare_target(self) -> bool:
        ctx, git, batch_branch = self.ctx, self.git, self.batch_branch
        if not batch_branch:
            return True
        paths = git.changed_paths(self.start_ref)
        mode = review_mode_for(paths, ctx.config)
        destination = (
            batch_branch if mode == "batch" else ctx.config.base_branch if mode == "fast" else self.target_branch
        )
        if destination == self.target_branch:
            return True
        # Path overrides can select batch after implementation or generated
        # files, or return to fast after a retry removes those paths.
        if destination == batch_branch:
            ctx.git.ensure_batch_branch(batch_branch, self.base_ref)
        with tempfile.NamedTemporaryFile() as patch:
            git.write_staged_patch(self.start_ref, Path(patch.name))
            self.start_ref = f"origin/{destination}"
            self.target_branch = destination
            self.recreate_worktree()
            if not _acceptance_matches(self.n, ctx, git, self.approval):
                return False
            try:
                git.apply_staged_patch(Path(patch.name))
            except GitError as exc:
                raise PatchReplayError(str(exc)) from exc
        return True

    def recover_replay(self, exc: PatchReplayError) -> str:
        # A three-way failure can leave unmerged index entries. Start the
        # next implementation attempt from the destination's clean tree.
        self.recreate_worktree()
        self.paths = []
        return f"Could not apply child changes to {self.target_branch}: {exc}"

    def rebuild(self, exc: ConflictBudgetExhausted) -> str:
        n, ctx, git, log = self.n, self.ctx, self.git, self.log
        self.rebuilt = True
        _append(log, f"Rebuilding issue branch once from latest {self.target_branch}.\n{exc}")
        ctx.git.fetch("origin", self.target_branch)
        self.start_ref = f"origin/{self.target_branch}"
        if not _acceptance_matches(n, ctx, ctx.git, self.approval, self.start_ref):
            raise ExecutorStopped("hash-mismatch")
        # Bind every rewritten push to the remote head observed before rebuilding.
        # An empty lease means the branch must still be absent.
        self.rebuild_lease = git.remote_branch_sha(self.branch) or ""
        _append(log, f"Rebuild target: {self.start_ref} ({git.ref_sha(self.start_ref)})")
        self.recreate_worktree()
        self.paths = []
        run = ctx.store.get_run(n)
        ctx.store.update_run(n, attempts=run.attempts + 1)
        return (
            f"The earlier attempt could not be merged onto the moved base ({self.target_branch}). "
            "The issue branch and worktree have been rebuilt from the latest target branch. "
            "Implement the original instructions again, preserving the features now on the base."
        )

    def push_branch(self) -> None:
        git = self.git
        if self.rebuilt:
            git.push(self.branch, lease=self.rebuild_lease)
            self.rebuild_lease = git.head_sha()
        else:
            git.push(self.branch)
        self.pushed_head = git.head_sha()

    def block_rebuild(self, details: str) -> str:
        _append(self.log, f"Rebuilt attempt failed:\n{details}")
        flag(self.ctx, self.n, Reason.RETRIES_EXCEEDED, "Rebuilt implementation failed; no attempts remain.", details)
        return "retries-exceeded"

    def verify(self) -> str:
        n, ctx, git, worktree, log, baseline = self.n, self.ctx, self.git, self.worktree, self.log, self.baseline
        _phase(n, ctx, "checking")
        git.stage_changes()
        if not self.prepare_target():
            raise ExecutorStopped("hash-mismatch")
        self.paths = git.changed_paths(self.start_ref)
        _progress(n, ctx)
        self.checks = []
        if not self.paths:
            return "The session made no changes."
        staged_tree = git.staged_tree_sha()
        for verification in range(2):
            self.checks = checks = []
            for command in self.parsed.done_when_commands:
                checks.append(_check(command, worktree, log, 900, progress=lambda: _progress(n, ctx),
                                     slot=_test_slot(n, ctx)))
                _progress(n, ctx)
            if baseline and baseline.node_ids:
                command = f"{baseline.command} {shlex.join(baseline.node_ids)}"
                result = _check(command, worktree, log, 900, progress=lambda: _progress(n, ctx),
                                slot=_test_slot(n, ctx))
                checks.append(result)
                _append(log, f"Acceptance baseline verification count={len(baseline.node_ids)}, "
                        f"exit_code={result.exit_code}, timed_out={result.timed_out}")
                _progress(n, ctx)
            test_command = ctx.config.test_command
            if test_command is None:
                test_command = next(iter(detect_test_commands(worktree)), None)
            unit_command = _pytest_command(test_command) if test_command else None
            unit_tests = _related_unit_tests(worktree, self.paths, ctx.config.acceptance_dir)
            if unit_tests and unit_command:
                limit = max(900, _remaining(n, ctx))
                result = _check(f"{unit_command} {shlex.join(unit_tests)}", worktree, log, limit,
                                progress=lambda: _progress(n, ctx), slot=_test_slot(n, ctx))
                checks.append(result)
                _progress(n, ctx)
                if result.timed_out:
                    detail = f"Related unit tests timed out after {limit} s ({len(unit_tests)} selected tests)."
                    flag(ctx, n, Reason.TIMEOUT, detail)
                    raise ExecutorStopped("timeout", detail)
            elif unit_tests:
                _append(log, "Related unit tests skipped: no supported pytest command is known")
            git.stage_changes()
            if not self.prepare_target():
                raise ExecutorStopped("hash-mismatch")
            self.paths = git.changed_paths(self.start_ref)
            checked_tree = git.staged_tree_sha()
            if checked_tree != staged_tree:
                if verification == 1:
                    return "Done when commands modify tracked files"
                _append(log, "Done when changed staged contents; checking the final contents again.")
                staged_tree = checked_tree
                continue
            failures = [check for check in checks if check.exit_code != 0 or check.timed_out]
            if failures:
                check = failures[-1]
                return f"Command: {check.command}\n{check.output}" + ("\nCheck timed out." if check.timed_out else "")
            try:
                _check_publication(git, self.target_branch)
            except AttemptFailure as exc:
                return str(exc)
            return "" if self.paths else "The session made no changes."
        raise AssertionError("unreachable verification state")

    def render_pr_body(self, try_text: str | None = None) -> str:
        n, ctx, paths = self.n, self.ctx, self.paths
        lane = ctx.store.get_run(n).lane
        globs = ctx.config.lanes.get(lane, {}).get("paths", []) if lane != "main" else None
        inside = set(touched(paths, globs)) if globs is not None else set(paths)
        outside = [path for path in paths if path not in inside]
        return _pr_body(
            n, self.parsed, self.checks, ctx.store.get_run(n).attempts, self.reviews, try_text, outside_lane=outside,
            fallback_model=ctx.store.get_run(n).fallback_model,
            attempt_tiers=ctx.store.get_run(n).attempt_tiers,
            missing_skills=self.missing_skills,
            rebuttals="\n\n".join(self.rebuttals),
            review_scope=ctx.config.review_scope, acceptance_dir=ctx.config.acceptance_dir,
        )

    def open_or_update_pr(self, title: str, body: str) -> int:
        ctx = self.ctx
        _check_publication(self.git, self.target_branch)
        pr = ctx.tracker.find_pr(self.branch)
        if pr is None:
            pr = ctx.tracker.create_pr(head=self.branch, base=self.target_branch, title=title, body=body)
        else:
            ctx.tracker.update_pr(pr, base=self.target_branch, title=title, body=body)
        board.sync_status(ctx, self.n, "PR open")
        return pr

    def resolve_conflicts(self, conflicts: list[str]) -> None:
        n, ctx, git = self.n, self.ctx, self.git
        conflicts = sorted(set(conflicts) | set(ctx.store.get_run(n).merge_conflicts))
        ctx.store.update_run(n, merge_conflicts=conflicts)
        title = f"{self.issue.title} (#{n})"
        pending_merge = git.merge_in_progress()
        check_failure = f"Merge conflict with {self.target_branch}: {', '.join(conflicts)}"
        while True:
            if self.rebuilt:
                raise ExecutorStopped(self.block_rebuild(check_failure))
            if self.conflict_attempts >= ctx.config.conflict_retries:
                raise ConflictBudgetExhausted(check_failure)
            self.conflict_attempts += 1
            _append(self.log, f"Conflict attempt {self.conflict_attempts}/{ctx.config.conflict_retries}:\n{check_failure}")
            self.implement(check_failure, merge_conflicts=conflicts)
            # Retain the merge's paths even if the executor stages them.
            markers = git.conflict_marker_paths(conflicts)
            if markers:
                check_failure = f"Conflict markers remain in: {', '.join(markers)}"
                continue
            git.stage_changes()
            if pending_merge:
                git.continue_merge()
                pending_merge = False
            elif git.has_staged_changes():
                git.commit(title)
            check_failure = self.verify()
            markers = git.conflict_marker_paths(conflicts)
            if markers:
                check_failure = f"Conflict markers remain in: {', '.join(markers)}"
            if not check_failure:
                ctx.store.update_run(n, merge_conflicts=[])
                return
