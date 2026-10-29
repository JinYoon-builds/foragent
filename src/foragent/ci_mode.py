"""Consume complete PR-head acceptance evidence before an issue PR can merge."""

from dataclasses import dataclass
import json
from math import isfinite

from foragent.actions import flag
from foragent.ci_workflow import EXCLUSIONS_TRAILER, WORKFLOW, acceptance_command, measurement_scope, read_exclusions
from foragent.git import GitError
from foragent.reasons import Reason
from foragent.run_attempt import AttemptFailure, ExecutorStopped
from foragent.run_baseline import _baseline_exclusions
from foragent.tracker import TrackerError


class UnusableComparison(ValueError):
    pass


class MissingComparison(UnusableComparison):
    pass


def _excluded_subset(value, required):
    """CI's exclusions still protect every file the gate requires now."""
    return (isinstance(value, list) and all(isinstance(path, str) for path in value)
            and set(value) <= set(required))


def scope_covers(recorded, required):
    """Equal scopes except that recorded exclusions may be a subset of required ones."""
    if not isinstance(recorded, dict) or not isinstance(required, dict) or recorded.keys() != required.keys():
        return False
    return (all(recorded[key] == required[key] for key in required if key != "excluded")
            and _excluded_subset(recorded.get("excluded"), required.get("excluded", [])))


def _trusted_sha(git, tracker):
    try:
        return git.ref_sha(f"origin/{tracker.pr_default_branch()}")
    except (GitError, AttributeError, TrackerError) as exc:
        if isinstance(exc, TrackerError) and exc.is_rate_limit:
            raise
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
    reports: str | None = None

    def current(self, n, ctx, git, pr, head, base_sha, status):
        """One non-waiting read; changed evidence must be checked again unlocked."""
        if (self.base_sha != base_sha or self.head_sha != head or self.status != status
                or self.trusted_sha != _trusted_sha(git, ctx.tracker)):
            return False
        excluded = _baseline_exclusions(ctx, base_sha, ctx.store.log_path(n))
        if not scope_covers(self.scope, measurement_scope(ctx.config, ctx.repo_root, excluded)):
            return False
        try:
            if self.infrastructure and status == "fail":
                logs = ctx.tracker.pr_failed_logs(pr)
                if not getattr(logs, "not_started", False) or str(logs) != self.infrastructure:
                    return False
            if self.reports is None:
                return True  # Local evidence is pinned to the same base/head and scope.
            return self.reports == json.dumps(ctx.tracker.pr_acceptance_reports(head), sort_keys=True)
        except (AttributeError, TrackerError) as exc:
            if isinstance(exc, TrackerError) and exc.is_rate_limit:
                raise
            return False


def enabled(ctx, target_branch):
    return (ctx.config.checks == "ci" and target_branch == ctx.config.base_branch
            and acceptance_command(ctx.config, ctx.repo_root) is not None)


def commit_exclusions(attempt, title):
    """Carry the current selection on a content/merge commit, or one empty commit."""
    git = attempt.git
    committing = git.has_staged_changes() or git.merge_in_progress()
    if enabled(attempt.ctx, attempt.target_branch):
        excluded = _baseline_exclusions(attempt.ctx, git.ref_sha(attempt.start_ref), attempt.log)
        excluded = sorted(set(excluded))
        if not committing:
            try:
                if read_exclusions(git, attempt.start_ref) == excluded:
                    return
            except ValueError:
                pass  # Replace an invalid selection before publishing this head.
        title += f"\n\n{EXCLUSIONS_TRAILER}: {json.dumps(excluded, separators=(',', ':'))}"
    elif not committing:
        return
    git.commit(title, allow_empty=not committing)


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


def comparison(artifacts, repo, base_sha, head_sha, excluded, *, expected_scope, expected_shards):
    reports = {}
    expected_shards = set(expected_shards)
    producer = None
    duration = 0
    lost = set()
    seen = {"base": set(), "head": set()}
    if not isinstance(artifacts, list):
        raise UnusableComparison("invalid artifact listing")
    artifact_ids = set()
    excluded = set(excluded)
    for evidence in artifacts:
        try:
            run, artifact, payload = evidence["run"], evidence["artifact"], evidence["report"]
            run_id, artifact_id = run["id"], artifact["id"]
            index, shards = payload["shard_index"], payload["shard_count"]
            if (type(run_id) is not int or run_id <= 0
                    or run["head_sha"] != head_sha or run["status"] != "completed"
                    or run["conclusion"] != "success" or run["event"] != "pull_request"
                    or run["path"].split("@", 1)[0] != WORKFLOW
                    or run["repository"]["full_name"] != repo
                    or run["head_repository"]["full_name"] != repo
                    or type(artifact_id) is not int or artifact_id <= 0 or artifact_id in artifact_ids
                    or artifact["expired"] is not False
                    or type(artifact["workflow_run"]["id"]) is not int
                    or artifact["workflow_run"]["id"] != run_id
                    or artifact["workflow_run"]["head_sha"] != head_sha
                    or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                    or payload["repository"] != repo or payload["base_sha"] != base_sha
                    or payload["head_sha"] != head_sha or not _excluded_subset(payload["excluded"], excluded)
                    or not scope_covers(payload["scope"], expected_scope)
                    or payload["scope"]["excluded"] != payload["excluded"]
                    or type(index) is not int or type(shards) is not int or not 1 <= shards <= 256
                    or shards != len(expected_shards) or index not in expected_shards
                    or not 0 <= index < shards or artifact["name"] != f"acceptance-{index}"
                    or index in reports or (producer is not None and producer != run_id)):
                raise UnusableComparison("shard identity, status or exclusions do not match")
            artifact_ids.add(artifact_id)
            seconds = payload["duration_seconds"]
            if type(seconds) not in (int, float) or not isfinite(seconds) or seconds < 0:
                raise UnusableComparison("invalid duration")
            producer = run_id
            base, head = suite(payload["base"]), suite(payload["head"])
            ci_excluded = set(payload["excluded"])
            for side, ids in (("base", base[0]), ("head", head[0])):
                if seen[side] & ids or any(node.split("::", 1)[0] in ci_excluded for node in ids):
                    raise UnusableComparison("overlapping or excluded tests")
                seen[side].update(ids)
            # Files excluded since CI started are no longer protected.
            lost.update(node for node in base[1] - head[1] if node.split("::", 1)[0] not in excluded)
            reports[index] = payload
            duration = max(duration, seconds)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise UnusableComparison(f"Unusable acceptance shard: {exc}") from exc
    if not expected_shards or set(reports) != expected_shards:
        raise MissingComparison("missing acceptance shards")
    return sorted(lost), duration


def require_comparison(attempt, pr, head, infrastructure="", *, base_sha=None, status="pass", deadline=None):
    from foragent.ci_local import LocalComparisonUnavailable, fallback_comparison

    n, ctx = attempt.n, attempt.ctx
    if deadline is None:
        deadline = ctx.now().timestamp() + 1200
    if base_sha is None:
        base_sha = attempt.git.ref_sha(f"origin/{attempt.target_branch}")
    excluded = _baseline_exclusions(ctx, base_sha, attempt.log)
    scope = measurement_scope(ctx.config, ctx.repo_root, excluded)
    trusted_sha = _trusted_sha(attempt.git, ctx.tracker)
    evidence = ComparisonEvidence(base_sha, head, scope, trusted_sha, status, infrastructure)
    # pull_request executes the head's workflow. Its artifacts are usable only
    # when all acceptance workflows match the default branch exactly.
    workflow_error = None
    try:
        if trusted_sha is None:
            raise GitError("Default branch reference unavailable")
        changed_workflows = attempt.git._call([
            "diff", "--name-only", "-z", trusted_sha, head, "--",
            ".github/workflows/foragent-acceptance*.yml",
            ".github/workflows/foragent-acceptance*.yaml",
        ])
    except GitError as exc:
        changed_workflows = False
        workflow_error = str(exc)
    if changed_workflows:
        try:
            fallback_comparison(attempt, base_sha)
            return evidence
        except LocalComparisonUnavailable as exc:
            flag(ctx, n, Reason.ACCEPTANCE_BASELINE_UNAVAILABLE, "Local acceptance comparison unavailable", str(exc))
            raise ExecutorStopped("held", str(exc)) from exc
    try:
        if workflow_error:
            raise UnusableComparison(workflow_error)
        # Measurement and artifact registration share ordinary CI's deadline.
        while True:
            reports = ctx.tracker.pr_acceptance_reports(head)
            try:
                lost, duration = comparison(reports, ctx.repo, base_sha, head, excluded,
                                           expected_scope=scope,
                                           expected_shards=range(ctx.config.ci_shards))
                break
            except MissingComparison:
                from foragent import runner

                remaining = min(deadline - ctx.now().timestamp(), runner._wait_remaining(n, ctx))
                if infrastructure or remaining <= 0:
                    raise
                from foragent.run_waits import _progress

                _progress(n, ctx)
                ctx.sleep(min(30, remaining))
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
                              json.dumps(reports, sort_keys=True))


def review_checks(n, ctx, git, target_branch):
    """Rechecking an existing issue PR uses the same CI and machine evidence."""
    from types import SimpleNamespace
    from foragent import runner
    from foragent.ci_local import capture_local_only, check, verify_local_only_units
    from foragent.run_checks import _related_unit_tests, run_related_check, trusts_ci
    from foragent.run_waits import in_phase
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
    with in_phase(n, ctx, "checking"):
        local_units = verify_local_only_units(attempt, baseline.command)
        checks = [local_units] if local_units is not None else []
        changed = git.changed_paths(base)
        own = _related_unit_tests(git.root, changed, ctx.config.acceptance_dir,
                                  own_only=ctx.config.runner_unit_tests != "all"
                                  and (enabled(ctx, target_branch) or trusts_ci(git.root, ctx.config)))
        if baseline.node_ids:
            checks.append(check(n, ctx, f"{baseline.command} {shlex.join(baseline.node_ids)}", git.root))
        if own:
            # Related tests are a recorded check of their own, as in an implementation's verification.
            checks.append(run_related_check(n, ctx, git.root, baseline.command, own, len(changed),
                                            max(1, runner._remaining(n, ctx))))
    for result in checks:
        if result.exit_code != 0 or result.timed_out:
            raise AttemptFailure(f"{result.command}\n{result.output}", target_branch)
    return attempt
