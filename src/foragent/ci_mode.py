"""Consume complete PR-head acceptance evidence before an issue PR can merge."""

from dataclasses import dataclass
import json
from math import isfinite
import zlib

from foragent.actions import flag
from foragent.ci_workflow import EXCLUSIONS, PUBLISH_WORKFLOW, acceptance_command, measurement_scope, producer_run_id
from foragent.ci_transport import decode_report
from foragent.git import GitError
from foragent.reasons import Reason
from foragent.run_attempt import AttemptFailure, ExecutorStopped
from foragent.run_baseline import _baseline_exclusions
from foragent.tracker import TrackerError


class UnusableComparison(ValueError):
    pass


class MissingComparison(UnusableComparison):
    pass


def _trusted_sha(git):
    try:
        return git.ref_sha("origin/HEAD")
    except GitError:
        return None


@dataclass(frozen=True)
class ComparisonEvidence:
    """A completed comparison and the mutable inputs it actually verified."""

    base_sha: str
    head_sha: str
    scope: dict
    trusted_sha: str | None
    status: str
    infrastructure: str
    checks: str | None = None

    def current(self, n, ctx, git, pr, head, base_sha, status):
        """One non-waiting read; changed evidence must be checked again unlocked."""
        if (self.base_sha != base_sha or self.head_sha != head or self.status != status
                or self.trusted_sha != _trusted_sha(git)):
            return False
        excluded = _baseline_exclusions(ctx, base_sha, ctx.store.log_path(n))
        if self.scope != measurement_scope(ctx.config, ctx.repo_root, excluded):
            return False
        try:
            if self.infrastructure and status == "fail":
                logs = ctx.tracker.pr_failed_logs(pr)
                if not getattr(logs, "not_started", False) or str(logs) != self.infrastructure:
                    return False
            if self.checks is None:
                return True  # Local evidence is pinned to the same base/head and scope.
            return self.checks == json.dumps(ctx.tracker.pr_acceptance_checks(pr), sort_keys=True)
        except (AttributeError, TrackerError) as exc:
            if isinstance(exc, TrackerError) and exc.is_rate_limit:
                raise
            return False


def enabled(ctx, target_branch):
    return (ctx.config.checks == "ci" and target_branch == ctx.config.base_branch
            and acceptance_command(ctx.config, ctx.repo_root) is not None)


def stage_exclusions(attempt):
    """Publish the runner's frozen selection, including when .foragent is ignored."""
    if not enabled(attempt.ctx, attempt.target_branch):
        return
    path = attempt.worktree / EXCLUSIONS
    path.parent.mkdir(parents=True, exist_ok=True)
    excluded = _baseline_exclusions(attempt.ctx, attempt.git.ref_sha(attempt.start_ref), attempt.log)
    path.write_text(json.dumps(excluded) + "\n", encoding="utf-8")
    attempt.git.publication_paths.add(EXCLUSIONS)
    attempt.git._call(["add", "--force", "--", EXCLUSIONS])


def suite(value):
    if not isinstance(value, dict) or value.get("complete") is not True:
        raise UnusableComparison("incomplete suite")
    sets = []
    for key in ("collected_ids", "passing_ids"):
        ids = value.get(key)
        if not isinstance(ids, list) or any(not isinstance(node, str) or "::" not in node for node in ids):
            raise UnusableComparison(f"invalid {key}")
        if len(ids) != len(set(ids)):
            raise UnusableComparison(f"duplicate {key}")
        sets.append(set(ids))
    if not sets[1] <= sets[0]:
        raise UnusableComparison("passing tests were not collected")
    return sets


def comparison(checks, repo, base_sha, head_sha, excluded, *, expected_scope, expected_shards):
    reports = {}
    expected_shards = set(expected_shards)
    producer = None
    duration = 0
    lost = set()
    seen = {"base": set(), "head": set()}
    if not isinstance(checks, list):
        raise UnusableComparison("invalid check-run listing")
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("name"), str):
            raise UnusableComparison("invalid check run")
        name = check.get("name", "")
        if not name.startswith("foragent-acceptance/"):
            continue
        run_id = producer_run_id(check, repo)
        run = check.get("workflow_run")
        if not isinstance(run, dict):
            continue
        if (run_id is None or run.get("id") != run_id
                or (run.get("repository") or {}).get("full_name") != repo
                or run.get("path", "").split("@", 1)[0] != PUBLISH_WORKFLOW
                or run.get("event") != "workflow_run"):
            continue
        try:
            payload = decode_report(check["output"]["text"])
            index, shards = payload["shard_index"], payload["shard_count"]
            if (check.get("head_sha") != head_sha or check.get("status") != "completed"
                    or check.get("conclusion") != "success" or type(payload["schema_version"]) is not int
                    or payload["schema_version"] != 1
                    or payload["repository"] != repo or payload["base_sha"] != base_sha
                    or payload["head_sha"] != head_sha or payload["excluded"] != list(excluded)
                    or payload["scope"] != expected_scope
                    or type(index) is not int or type(shards) is not int or not 1 <= shards <= 256
                    or shards != len(expected_shards) or index not in expected_shards
                    or not 0 <= index < shards or name != f"foragent-acceptance/{index}"
                    or index in reports or (producer is not None and producer != run_id)):
                raise UnusableComparison("shard identity, status or exclusions do not match")
            seconds = payload["duration_seconds"]
            if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
                raise UnusableComparison("invalid duration")
            producer = run_id
            base, head = suite(payload["base"]), suite(payload["head"])
            for side, ids in (("base", base[0]), ("head", head[0])):
                if seen[side] & ids or any(node.split("::", 1)[0] in excluded for node in ids):
                    raise UnusableComparison("overlapping or excluded tests")
                seen[side].update(ids)
            lost.update(base[1] - head[1])
            reports[index] = payload
            duration = max(duration, seconds)
        except (KeyError, TypeError, ValueError, zlib.error) as exc:
            raise UnusableComparison(f"Unusable acceptance shard {name}: {exc}") from exc
    if not expected_shards or set(reports) != expected_shards:
        raise MissingComparison("missing acceptance shards")
    return sorted(lost), duration


def require_comparison(attempt, pr, head, infrastructure="", *, base_sha=None, status="pass"):
    from foragent.ci_local import LocalComparisonUnavailable, fallback_comparison

    n, ctx = attempt.n, attempt.ctx
    if base_sha is None:
        base_sha = attempt.git.ref_sha(f"origin/{attempt.target_branch}")
    excluded = _baseline_exclusions(ctx, base_sha, attempt.log)
    scope = measurement_scope(ctx.config, ctx.repo_root, excluded)
    trusted_sha = _trusted_sha(attempt.git)
    evidence = ComparisonEvidence(base_sha, head, scope, trusted_sha, status, infrastructure)
    # pull_request executes the head's workflow. Its artifacts are usable only
    # when all acceptance workflows match the default branch exactly. A missing
    # remote default-branch reference also requires a local comparison.
    try:
        if trusted_sha is None:
            raise GitError("Default branch reference unavailable")
        changed_workflows = attempt.git._call([
            "diff", "--name-only", "-z", trusted_sha, head, "--",
            ".github/workflows/foragent-acceptance*.yml",
            ".github/workflows/foragent-acceptance*.yaml",
        ])
    except GitError:
        changed_workflows = True
    if changed_workflows:
        try:
            fallback_comparison(attempt, base_sha)
            return evidence
        except LocalComparisonUnavailable as exc:
            flag(ctx, n, Reason.ACCEPTANCE_BASELINE_UNAVAILABLE, "Local acceptance comparison unavailable", str(exc))
            raise ExecutorStopped("held", str(exc)) from exc
    try:
        # workflow_run publication follows the measurement run asynchronously.
        # Give its checks the same registration grace as ordinary Actions jobs.
        for poll in range(7):
            checks = ctx.tracker.pr_acceptance_checks(pr)
            try:
                lost, duration = comparison(checks, ctx.repo, base_sha, head, excluded,
                                           expected_scope=scope,
                                           expected_shards=range(ctx.config.ci_shards))
                break
            except MissingComparison:
                if infrastructure or poll == 6:
                    raise
                from foragent.run_waits import _progress

                _progress(n, ctx)
                ctx.sleep(30)
    except (UnusableComparison, AttributeError, TrackerError) as exc:
        if isinstance(exc, TrackerError) and exc.is_rate_limit:
            raise
        detail = f"{exc}\n{infrastructure}".strip()
        if ctx.config.checks_fallback == "local":
            try:
                fallback_comparison(attempt, base_sha)
                return evidence
            except LocalComparisonUnavailable as fallback:
                detail += f"\n{fallback}"
        flag(ctx, n, Reason.ACCEPTANCE_BASELINE_UNAVAILABLE, "Acceptance comparison unavailable", detail)
        raise ExecutorStopped("held", detail) from exc
    ctx.store.update_run(n, comparison={"source": "ci", "duration_seconds": duration})
    if lost:
        raise AttemptFailure("Acceptance baseline regression (previously passing tests):\n" + "\n".join(lost),
                             attempt.target_branch)
    if infrastructure:
        if ctx.config.checks_fallback == "local":
            try:
                fallback_comparison(attempt, base_sha)
                return evidence
            except LocalComparisonUnavailable as exc:
                infrastructure += f"\n{exc}"
        flag(ctx, n, Reason.CI_NOT_STARTED, "CI jobs did not start", infrastructure)
        raise ExecutorStopped("held", infrastructure)
    return ComparisonEvidence(base_sha, head, scope, trusted_sha, status, infrastructure,
                              json.dumps(checks, sort_keys=True))


def review_checks(n, ctx, git, target_branch):
    """Rechecking an existing issue PR uses the same CI and machine evidence."""
    from types import SimpleNamespace
    from foragent import runner
    from foragent.ci_local import capture_local_only, check, verify_local_only_units
    from foragent.run_checks import _related_unit_tests, trusts_ci
    import shlex

    base = f"origin/{target_branch}"
    command = ctx.config.test_command or next(iter(runner.detect_test_commands(git.root)), None)
    try:
        baseline = capture_local_only(n, ctx, git, command, git.ref_sha(base),
                                      _baseline_exclusions(ctx, git.ref_sha(base), ctx.store.log_path(n)))
    except AttemptFailure as exc:
        flag(ctx, n, Reason.ACCEPTANCE_BASELINE_UNAVAILABLE, "local_only acceptance baseline unavailable", str(exc))
        raise ExecutorStopped("held", str(exc)) from exc
    attempt = SimpleNamespace(n=n, ctx=ctx, git=git, worktree=git.root, target_branch=target_branch,
                              baseline=baseline, log=ctx.store.log_path(n))
    checks = [verify_local_only_units(attempt, baseline.command)]
    own = _related_unit_tests(git.root, git.changed_paths(base), ctx.config.acceptance_dir,
                              own_only=ctx.config.runner_unit_tests != "all"
                              and (enabled(ctx, target_branch) or trusts_ci(git.root, ctx.config)))
    ids = [*baseline.node_ids, *own]
    if ids:
        checks.append(check(n, ctx, f"{baseline.command} {shlex.join(ids)}", git.root))
    for result in checks:
        if result.exit_code != 0 or result.timed_out:
            raise AttemptFailure(f"{result.command}\n{result.output}", target_branch)
    return attempt
