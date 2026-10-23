"""Publication, merge and retry loop of one runner attempt."""

from dataclasses import replace
from foragent import board
from foragent.actions import flag
from foragent.git import GitError, GitRepo
from foragent.labels import READY, RUNNING
from foragent.orch_reinstall import after_merge
from foragent.policy import merge_decision
from foragent.reasons import Reason
from foragent.tracker import TrackerError
from foragent.runner_core import AttemptFailure, CIFailure, ConflictBudgetExhausted, ExecutorStopped, PatchReplayError, RunContext, _append, _ci_failure, _hold_changed_head, _merge_lock, _phase, _progress, _remaining, _timeout
from foragent.runner_prompts import _normalized_findings, _read_rebuttals
from foragent.runner_review import _acceptance_matches, _batch_pr, _check_publication, _inject_lane_skills, _next_review_round, _preserve_failed_implementation, _review
from foragent.runner_attempt import _Attempt


class _Publication(_Attempt):
    """Publish, merge and drive the retry loop of one implementation attempt."""

    def publish(self) -> str:
        with _merge_lock(self.n, self.ctx):
            return self.publish_locked()

    def publish_locked(self) -> str:
        n, ctx, git, worktree, log = self.n, self.ctx, self.git, self.worktree, self.log
        batch_branch, parent = self.batch_branch, self.parent
        # Only metadata is read live; instructions and checks always use the snapshot.
        issue = ctx.tracker.get_issue(n)
        title = f"{issue.title} (#{n})"
        if git.has_staged_changes():
            git.commit(title)
        _progress(n, ctx)
        verified_head_sha = git.head_sha()
        base_merges = 0

        def refresh_base() -> str | None:
            nonlocal verified_head_sha, base_merges
            ctx.git.fetch("origin", self.target_branch)
            target_ref = f"origin/{self.target_branch}"
            target_sha = git.ref_sha(target_ref)
            if git.is_ancestor(target_sha, verified_head_sha):
                return None
            if base_merges == 3:
                flag(ctx, n, Reason.TIMEOUT, "base kept moving")
                raise ExecutorStopped("timeout")
            _inject_lane_skills(ctx, n, git, target_ref)
            try:
                git.merge_base(target_ref)
            except GitError:
                conflicts = git.conflict_paths()
                if not conflicts:
                    raise
                self.resolve_conflicts(conflicts)
            else:
                check_failure = self.verify()
                if check_failure:
                    raise AttemptFailure(check_failure)
            base_merges += 1
            if _remaining(n, ctx) <= 0:
                raise ExecutorStopped(_timeout(n, ctx))
            if git.has_staged_changes():
                git.commit(title)
            verified_head_sha = git.head_sha()
            return target_sha

        target_sha = refresh_base() if not self.review_hold else None
        if not self.review_hold and _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        _check_publication(git, self.target_branch)
        self.push_branch()
        _progress(n, ctx)
        _append(log, f"Pushed verified HEAD: {verified_head_sha}")
        try_text = None
        try_path = worktree / ".foragent/TRY.md"
        if not self.review_hold and try_path.is_file():
            try_text = try_path.read_bytes().decode("utf-8", errors="replace")
            saved_try = ctx.store.root / "try" / f"{n}.md"
            saved_try.parent.mkdir(parents=True, exist_ok=True)
            saved_try.write_text(try_text, encoding="utf-8")

        def pr_body() -> str:
            return self.render_pr_body(try_text)

        pr = self.open_or_update_pr(title, pr_body())
        if target_sha is not None:
            ctx.tracker.comment(pr, f"base merged before merge: {target_sha}")
        _progress(n, ctx)
        pr_ready_at = ctx.now().timestamp()
        link = f"https://github.com/{ctx.repo}/pull/{pr}"
        if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
            return _hold_changed_head(n, ctx, link)
        if self.review_hold:
            summary = (
                "Reviewer repeated a disputed finding after implementer rebuttals"
                if self.review_hold_reason == Reason.REVIEW_DISPUTE
                else f"Reviewer still found defects after {self.review_failures} rounds"
            )
            details = f"{self.failure}\n\n{link}"
            flag(ctx, n, self.review_hold_reason, summary, details)
            return "held"
        decision = merge_decision(
            changed_paths=ctx.tracker.pr_changed_files(pr),
            issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
        )
        if decision.kind == "hold":
            flag(ctx, n, decision.reason, decision.summary, link)
            return "held"
        if decision.kind == "hold_mode" and (decision.mode == "careful" or not batch_branch):
            ctx.store.update_run(n, detail=f"PR #{pr} is waiting for your review.")
            ctx.tracker.comment(n, f"PR #{pr} is waiting for your review\n\n{link}")
            ctx.tracker.remove_labels(n, [RUNNING])
            return "pr-open"

        # A bounded iteration count also works with a frozen test clock.
        for _ in range(4):
            _phase(n, ctx, "ci")
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
                # Actions checks can register after each push, including base merges.
                if status == "none" and has_workflows and ctx.now().timestamp() - pr_ready_at < 180:
                    status = "pending"
                if status in {"pass", "none"}:
                    break
                if status == "fail":
                    if self.rebuilt:
                        raise ExecutorStopped(self.block_rebuild(f"CI failed:\n{ctx.tracker.pr_failed_logs(pr)}"))
                    _ci_failure(n, ctx, pr, verified_head_sha, self.target_branch)
                remaining = deadline - ctx.now().timestamp()
                if remaining <= 0 or poll == 40:
                    flag(ctx, n, Reason.TIMEOUT, "CI did not finish", link)
                    return "timeout"
                ctx.sleep(min(30, remaining))

            target_sha = refresh_base()
            target_ref = f"origin/{self.target_branch}"
            if target_sha is not None:
                _check_publication(git, self.target_branch)
                self.push_branch()
                _progress(n, ctx)
                _append(log, f"Pushed verified HEAD: {verified_head_sha}")
                ctx.tracker.update_pr(pr, base=self.target_branch, title=title, body=pr_body())
                ctx.tracker.comment(pr, f"base merged before merge: {target_sha}")
                pr_ready_at = ctx.now().timestamp()
                if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                    return _hold_changed_head(n, ctx, link)
                decision = merge_decision(
                    changed_paths=git.changed_paths(target_ref),
                    issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
                )
                if decision.kind == "hold":
                    flag(ctx, n, decision.reason, decision.summary, link)
                    return "held"
                if decision.kind == "hold_mode" and (decision.mode == "careful" or not batch_branch):
                    ctx.store.update_run(n, detail=f"PR #{pr} is waiting for your review.")
                    ctx.tracker.comment(n, f"PR #{pr} is waiting for your review\n\n{link}")
                    ctx.tracker.remove_labels(n, [RUNNING])
                    return "pr-open"
                continue
            _check_publication(git, self.target_branch)
            _phase(n, ctx, "merging")
            try:
                ctx.tracker.merge_pr(pr, method="squash", expected_head_sha=verified_head_sha)
            except TrackerError as exc:
                if exc.is_rate_limit:
                    raise
                if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                    return _hold_changed_head(n, ctx, link)
                raise
            if decision.kind == "merge":
                after_merge(ctx, pr, self.target_branch)
            _progress(n, ctx)
            try:
                if self.target_branch == batch_branch:
                    ctx.tracker.close_issue(n, f"Merged into {batch_branch} (PR #{pr})")
                else:
                    ctx.tracker.close_issue(n)
            except TrackerError as exc:
                if exc.is_rate_limit:
                    raise
                if ctx.tracker.current_issue(n).state != "closed":
                    raise
            ctx.tracker.remove_labels(n, [READY, RUNNING])
            board.sync_status(ctx, n, "Done")
            if self.target_branch == batch_branch and parent is not None:
                try:
                    _batch_pr(parent, ctx)
                except (GitError, TrackerError) as exc:
                    if isinstance(exc, TrackerError) and exc.is_rate_limit:
                        raise
                    _append(log, f"Batch PR finalization deferred to tick: {exc}")
            _progress(n, ctx)
            return "merged"
        raise AssertionError("unreachable refresh state")

    def run(self) -> str:
        n, ctx, git, worktree, log, approval = self.n, self.ctx, self.git, self.worktree, self.log, self.approval
        approved_ref = self.start_ref
        reviewer_text: str | None = None
        previous_rebutted_findings: set[str] = set()
        tracker_available = True
        outcome = "error"
        try:
            resume_verified = False
            conflicts = ctx.store.get_run(n).merge_conflicts
            if conflicts or not git.is_ancestor(git.ref_sha(self.start_ref), git.head_sha()):
                # A saved implementation must see the same target as its acceptance
                # baseline before the worker starts. Keep its history and reuse the
                # publish path's conflict resolution and rebuild budget.
                with _merge_lock(n, ctx):
                    if not git.is_ancestor(git.ref_sha(self.start_ref), git.head_sha()):
                        try:
                            # A saved partial resolution still needs its marker checks,
                            # even when Git can merge the target without index conflicts.
                            git.merge_base(self.start_ref, commit=not conflicts)
                        except GitError:
                            unmerged = git.conflict_paths()
                            if not unmerged:
                                raise
                            conflicts = sorted(set(conflicts) | set(unmerged))
                    if conflicts:
                        try:
                            self.resolve_conflicts(conflicts)
                        except ConflictBudgetExhausted as exc:
                            self.failure = self.rebuild(exc)
                        else:
                            resume_verified = True
            if not _acceptance_matches(n, ctx, git, approval):
                raise ExecutorStopped("hash-mismatch")
            approved_ref = git.head_sha()
            while True:
                if resume_verified:
                    self.failure = ""
                    resume_verified = False
                else:
                    self.implement(self.failure, reviewer_text=reviewer_text)
                    try:
                        self.failure = self.verify()
                    except PatchReplayError as exc:
                        self.failure = self.recover_replay(exc)
                reviewer_text = None
                run = ctx.store.get_run(n)
                if not self.failure and ctx.config.reviewer != "none":
                    if _remaining(n, ctx) <= 0:
                        outcome = _timeout(n, ctx)
                        return outcome
                    pr = None
                    if ctx.config.review_scope == "pr-body":
                        title = f"{ctx.tracker.get_issue(n).title} (#{n})"
                        if git.has_staged_changes():
                            git.commit(title)
                        self.push_branch()
                        pr = self.open_or_update_pr(title, self.render_pr_body())
                        _progress(n, ctx)
                    review = _review(n, ctx, git, self.parsed, self.base_ref,
                                     round_number=_next_review_round(n, ctx), pr=pr)
                    if review.verdict == "UNUSABLE":
                        review = replace(review, findings=review.raw)
                    self.reviews.append(review)
                    if pr is not None:
                        ctx.tracker.comment(pr, "\n".join([
                            f"Round {len(self.reviews)}: VERDICT: {review.verdict}", review.findings, *review.notes,
                        ]))
                    if review.verdict != "PASS":
                        self.review_failures += 1
                        if review.verdict == "UNUSABLE":
                            reviewer_text = review.raw
                            self.failure = "A reviewer said (verbatim):\n" + review.raw
                        else:
                            self.failure = review.findings or "Reviewer found defects (no findings provided)."
                        findings = _normalized_findings(review) if _read_rebuttals(worktree).strip() else set()
                        if findings & previous_rebutted_findings:
                            self.review_hold = True
                            self.review_hold_reason = Reason.REVIEW_DISPUTE
                        previous_rebutted_findings = findings
                        if self.review_failures > ctx.config.review_max:
                            self.review_hold = True
                if self.rebuilt and self.failure:
                    outcome = self.block_rebuild(str(self.failure))
                    return outcome
                if not self.failure or self.review_hold:
                    try:
                        outcome = self.publish()
                        return outcome
                    except ConflictBudgetExhausted as exc:
                        self.failure = self.rebuild(exc)
                        continue
                    except CIFailure as exc:
                        if self.rebuilt:
                            outcome = self.block_rebuild(str(exc))
                            return outcome
                        self.failure = exc
                        continue
                    except (AttemptFailure, PatchReplayError) as exc:
                        self.failure = self.recover_replay(exc) if isinstance(exc, PatchReplayError) else str(exc)
                        run = ctx.store.get_run(n)
                if self.rebuilt:
                    outcome = self.block_rebuild(str(self.failure))
                    return outcome
                _append(log, f"Attempt {run.attempts} failed:\n{self.failure}")
                ctx.store.update_run(n, resume_reason=str(self.failure))
                if run.attempts < ctx.config.max_retries + 1:
                    ctx.store.update_run(n, attempts=run.attempts + 1)
                    continue
                flag(ctx, n, Reason.RETRIES_EXCEEDED, "Checks or review failed after all attempts.", self.failure)
                outcome = "retries-exceeded"
                return outcome
        except ExecutorStopped as exc:
            outcome = exc.outcome
            raise
        except TrackerError as exc:
            tracker_available = False
            outcome = "limit-wait" if exc.is_rate_limit else "error"
            raise
        except (SystemExit, KeyboardInterrupt):
            outcome = "killed"
            raise
        finally:
            if outcome != "merged":
                run = ctx.store.get_run(n)
                _preserve_failed_implementation(
                    n, ctx, git, run.outcome or outcome, approval=approval, approved_ref=approved_ref,
                    title=self.issue.title, pushed_head=self.pushed_head, tracker_available=tracker_available,
                    push_branch=self.push_branch,
                )


def _implement_and_publish(
    n: int, ctx: RunContext, git: GitRepo, *, target_branch: str, failure: str | CIFailure = "",
) -> str:
    """Run the common implementation/check/review/publish loop in an existing worktree."""
    return _Publication(n, ctx, git, target_branch, failure).run()
