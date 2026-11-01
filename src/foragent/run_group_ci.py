"""Leader-owned temporary PR measurement and member-owned CI evidence reuse."""

from dataclasses import dataclass
from hashlib import sha256

from foragent import ci_mode, run_group_check, run_group_evidence
from foragent.ci_workflow import measurement_scope
from foragent.git import GitError
from foragent.run_baseline import _baseline_exclusions
from foragent.tracker import TrackerError


def _report_digest(reports):
    return sha256(ci_mode.serialize_reports(reports).encode()).hexdigest()


def check(n, ctx, target, base_sha, combined, *, excluded):
    """Compare using the same exclusions captured for the combined commit's trailer."""
    from foragent import runner
    from foragent.run_checks import _append
    from foragent.run_publish_ci import _CIWait
    from foragent.run_waits import _phase, _progress, in_phase

    _phase(n, ctx, "checking")
    branch = f"foragent/group-{n}-{combined[:12]}"
    pr = None
    published = False
    metadata = {"combined_sha": combined}
    verdict, reason = "void", None
    interrupted = False
    try:
        if ci_mode.changed_workflows(ctx.git, ci_mode._trusted_sha(ctx.git, ctx.tracker), combined):
            raise ci_mode.UnusableComparison("group acceptance workflow differs from the default branch")
        scope = measurement_scope(ctx.config, ctx.repo_root, excluded)
        wait = _CIWait(ctx.now().timestamp())
        published = True  # Even a lost push response may have published the ref.
        ctx.git._call(["push", f"--force-with-lease=refs/heads/{branch}:",
                       "origin", f"{combined}:refs/heads/{branch}"])
        pr = ctx.tracker.create_pr(head=branch, base=target, title=f"Foragent group check: {n}",
                                   body=f"Temporary acceptance comparison on {base_sha}.")
        metadata["pr"] = pr
        with in_phase(n, ctx, "ci"):
            while True:
                reports = ctx.tracker.pr_acceptance_reports(combined)
                try:
                    lost, duration = ci_mode.comparison(
                        reports, ctx.repo, base_sha, combined, excluded, expected_scope=scope,
                        expected_shards=range(ctx.config.ci_shards))
                    break
                except (ci_mode.MissingComparison, ci_mode.StaleComparison):
                    remaining = min(wait.deadline - ctx.now().timestamp(), runner._remaining(n, ctx))
                    if remaining <= 0:
                        raise ci_mode.UnusableComparison("group CI evidence timed out")
                    _progress(n, ctx)
                    ctx.sleep(min(30, remaining))
        metadata.update(scope=scope, reports_sha256=_report_digest(reports), duration_seconds=duration,
                        run_ids=sorted({report["run"]["id"] for report in reports}))
        verdict = "failed" if lost else "passed"
        reason = "Acceptance baseline regression: " + ", ".join(lost) if lost else None
    except (KeyError, TypeError, ValueError, RecursionError, OverflowError,
            GitError, TrackerError, OSError, AttributeError) as exc:
        reason = str(exc).strip() or type(exc).__name__
    except BaseException:
        interrupted = True
        raise
    finally:
        if published:
            # Cleanup publishes Git changes too; CI waiting has ended. A cancellation cleans up without queueing.
            _phase(n, ctx, "checking", resume=not interrupted)
            # A PR creation failure can still leave a published ref (or an open PR
            # when the response was lost). Only this leader owns these resources.
            try:
                if pr is None:
                    pr = ctx.tracker.find_pr(branch)
                if pr is not None:
                    ctx.tracker.close_pr(pr)
            except (TrackerError, OSError) as exc:
                _append(ctx.store.log_path(n), f"Group PR cleanup: {exc}")
            finally:
                try:
                    if ctx.git.remote_branch_sha(branch) is not None:
                        ctx.git._call(["push", "origin", "--delete", branch])
                except (GitError, OSError) as exc:
                    _append(ctx.store.log_path(n), f"Group branch cleanup: {exc}")
    return verdict, reason, metadata


@dataclass(frozen=True)
class GroupComparisonEvidence:
    found: tuple
    neutral: run_group_evidence.Neutral
    infrastructure: str = ""

    def current(self, n, ctx, git, pr, head, base_sha, status):
        """Re-read the same member rule and the same CI evidence under the merge lock."""
        try:
            if run_group_evidence.void_reason(ctx, n, head, base_sha, self.found, self.neutral) is not None:
                return False
            event, _ = self.found
            combined = event["combined_sha"]
            if ci_mode.changed_workflows(git, ci_mode._trusted_sha(git, ctx.tracker), combined):
                return False
            excluded = _baseline_exclusions(ctx, base_sha, ctx.store.log_path(n))
            scope = measurement_scope(ctx.config, ctx.repo_root, excluded)
            if not ci_mode.scope_covers(event.get("scope"), scope):
                return False
            reports = ctx.tracker.pr_acceptance_reports(combined)
            if _report_digest(reports) != event.get("reports_sha256"):
                return False
            lost, _ = ci_mode.comparison(
                reports, ctx.repo, event["base_sha"], combined, excluded, expected_scope=scope,
                expected_shards=range(ctx.config.ci_shards))
            return not lost
        except (GitError, OSError, KeyError, TypeError, ValueError, RecursionError, OverflowError,
                AttributeError, TrackerError) as exc:
            if isinstance(exc, TrackerError) and exc.is_rate_limit:
                raise
            return False


def evidence(view, head, base_sha):
    """Use a CI group result only through #1141's single member reuse predicate."""
    n, ctx = view.n, view.ctx
    if run_group_check.group_limit(ctx.store, ctx.config)[0] <= 1:
        return None
    found = run_group_evidence.latest(ctx, n)
    if found is None or found[0].get("mode") != "ci":
        return None
    result = GroupComparisonEvidence(found, run_group_evidence.neutral_rule(view))
    return result if result.current(n, ctx, view.git, None, head, base_sha, "pass") else None
