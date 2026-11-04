"""Consume complete PR-head acceptance evidence before an issue PR can merge."""

from dataclasses import dataclass, replace
import json
from math import isfinite

from foragent.actions import flag
from foragent.ci_workflow import EXCLUSIONS_TRAILER, WORKFLOW, acceptance_command, measurement_scope, read_exclusions
from foragent.git import GitError
from foragent.reasons import Reason
from foragent.run_attempt import AttemptFailure, ExecutorStopped
from foragent.run_baseline import _baseline_exclusions
from foragent.tracker import RejectedEvidence, TrackerError


class UnusableComparison(ValueError):
    pass


class MissingComparison(UnusableComparison):
    pass


class StaleComparison(UnusableComparison):
    """Every check passed except the base or measurement producer is no longer current."""


class OlderMeasuredBase(StaleComparison):
    """The caller can refresh this head onto the expected base."""

    def __init__(self, reported):
        self.reported = reported
        super().__init__(reported)


class WorkflowChanged(UnusableComparison):
    """The measuring workflow differs from the default branch, so its artifacts are not trusted."""


def serialize_reports(reports):
    """Pin external evidence without letting malformed data escape as a code error."""
    try:
        return json.dumps(reports, sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        raise UnusableComparison(f"Unusable acceptance reports: {exc}") from exc


def _excluded_subset(value, required):
    """CI's exclusions still protect every file the gate requires now."""
    return _paths(value) and set(value) <= set(required)


def _paths(value):
    return isinstance(value, list) and all(isinstance(path, str) for path in value)


def scope_covers(recorded, required):
    """Equal scopes except that recorded exclusions may be a subset of required ones."""
    if not isinstance(recorded, dict) or not isinstance(required, dict) or recorded.keys() != required.keys():
        return False
    return (all(recorded[key] == required[key] for key in required if key != "excluded")
            and _excluded_subset(recorded.get("excluded"), required.get("excluded", [])))


def _trusted_sha(git, tracker, *, strict=False):
    try:
        return git.ref_sha(f"origin/{tracker.pr_default_branch()}")
    except (GitError, AttributeError, TrackerError) as exc:
        if isinstance(exc, TrackerError) and (strict or exc.waits):
            raise
        return None


def changed_workflows(git, trusted_sha, head):
    """PR artifacts are trusted only when their workflow matches the default branch."""
    if trusted_sha is None:
        raise GitError("Default branch reference unavailable")
    return git._call([
        "diff", "--name-only", "-z", trusted_sha, head, "--",
        ".github/workflows/foragent-acceptance*.yml",
        ".github/workflows/foragent-acceptance*.yaml",
    ])


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
    # A later base accepted by a proved result-neutral move; reports stay bound to base_sha.
    accepted_sha: str | None = None
    reports_by_head: dict | None = None

    def follow(self, base_sha):
        """The same evidence, accepted on a base the caller proved result-neutral."""
        return replace(self, accepted_sha=base_sha)

    def current(self, n, ctx, git, pr, head, base_sha, status):
        """One non-waiting read; changed evidence must be checked again unlocked."""
        refused = self._refused(n, ctx, git, pr, head, base_sha, status)
        _note_evidence(ctx, n, base_sha, head, *(refused or ()))
        return refused is None

    def _refused(self, n, ctx, git, pr, head, base_sha, status):
        """Why this read no longer accepts the evidence, as (reason, detail), or None."""
        accepted = self.accepted_sha or self.base_sha
        if accepted != base_sha or self.head_sha != head:
            return "revision-moved", f"accepted {accepted}/{self.head_sha}, now {base_sha}/{head}"
        if self.status != status:
            return "status-changed", f"accepted CI {self.status}, now {status}"
        trusted = (_trusted_sha(git, ctx.tracker, strict=True) if getattr(ctx.config, "merge_owner", False)
                   else _trusted_sha(git, ctx.tracker))
        if self.trusted_sha != trusted:
            return "trusted-moved", f"accepted default branch {self.trusted_sha}, now {trusted}"
        excluded = _baseline_exclusions(ctx, base_sha, ctx.store.log_path(n))
        if not scope_covers(self.scope, measurement_scope(ctx.config, ctx.repo_root, excluded)):
            return "scope-changed", "the measurement scope no longer covers the current one"
        try:
            if self.infrastructure and status == "fail":
                logs = ctx.tracker.pr_failed_logs(pr)
                if not getattr(logs, "not_started", False) or str(logs) != self.infrastructure:
                    return "infrastructure", "the not-started evidence changed"
            if self.reports is None:
                return None  # Local evidence is pinned to the same base/head and scope.
            for measured, recorded in (self.reports_by_head or {head: self.reports}).items():
                reports = ctx.tracker.pr_acceptance_reports(measured)
                if recorded != serialize_reports(reports):
                    return ("reports-changed", "the reports differ from the accepted ones") if reports else (
                        "missing", "no acceptance reports at this read")
            return None
        except (KeyError, TypeError, ValueError, RecursionError, OverflowError, AttributeError, TrackerError) as exc:
            if isinstance(exc, RejectedEvidence) and getattr(ctx.config, "merge_owner", False):
                return "unusable", str(exc)
            if isinstance(exc, TrackerError) and (getattr(ctx.config, "merge_owner", False) or exc.waits):
                raise
            return _refusal(exc), str(exc)


def ci_protection(ctx):
    """CI owns portable protection even when a target cannot produce evidence."""
    return (ctx.config.checks == "ci" and ctx.config.checks_fallback != "local"
            and acceptance_command(ctx.config, ctx.repo_root) is not None)


def ci_evidence_target(ctx, target_branch):
    """The generated acceptance workflow runs only against the configured base."""
    return target_branch == ctx.config.base_branch


def enabled(ctx, target_branch):
    return (ci_protection(ctx)
            or (ctx.config.checks == "ci" and ci_evidence_target(ctx, target_branch)
                and acceptance_command(ctx.config, ctx.repo_root) is not None))


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


def suite(value, path="suite"):
    """Collected and passing ids; a rejection names the field, observed and expected value."""
    if not isinstance(value, dict):
        raise UnusableComparison(f"{path}: observed {_shown(value)}, expected an object")
    complete = _shown(value["complete"]) if "complete" in value else "missing"
    if value.get("complete") is not True:
        raise UnusableComparison(f"incomplete suite: {path}.complete: observed {complete}, expected true")
    sets = []
    for key in ("collected_ids", "passing_ids"):
        if key not in value:
            raise UnusableComparison(f"{path}.{key}: observed missing, expected a list of test ids")
        ids = value[key]
        if not isinstance(ids, list):
            raise UnusableComparison(f"{path}.{key}: observed {_shown(ids)}, expected a list of test ids")
        for node in ids:
            if not isinstance(node, str) or "::" not in node:
                raise UnusableComparison(f"{path}.{key}: observed {_shown(node)}, expected a test id containing '::'")
        duplicate = next((node for position, node in enumerate(ids) if node in ids[:position]), None)
        if duplicate is not None:
            raise UnusableComparison(f"{path}.{key}: observed {_shown(duplicate)} twice, expected unique test ids")
        sets.append(set(ids))
    if not sets[1] <= sets[0]:
        node = sorted(sets[1] - sets[0])[0]
        raise UnusableComparison(f"{path}.passing_ids: observed {_shown(node)}, "
                                 f"expected only ids listed in {path}.collected_ids")
    return sets


def _shown(value):
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


def _field(evidence, path, expected="a value"):
    """Read a dotted path, naming the first missing or non-object step."""
    value, seen = evidence, []
    for key in path.split("."):
        if not isinstance(value, dict):
            where = ".".join(seen) or "shard"
            raise UnusableComparison(f"{where}: observed {_shown(value)}, expected an object")
        seen.append(key)
        if key not in value:
            raise UnusableComparison(f"{'.'.join(seen)}: observed missing, expected {expected}")
        value = value[key]
    return value


def _positive(value):
    return type(value) is int and value > 0


def comparison(artifacts, repo, base_sha, head_sha, excluded, *, expected_scope, expected_shards):
    """Validate every shard; the first failing field names its observed and expected value.

    Only a moved base or a superseded measurement producer is stale. Those wait
    until every other check passes, so an unrelated invalid field never retries.
    The exclusions CI recorded are the reference for a completed measurement;
    today's ``excluded`` may differ either way and neither waives nor adds a test.
    """
    reports = {}
    expected_shards = set(expected_shards)
    producer = None
    stale = None
    duration = 0
    lost = set()
    seen = {"base": set(), "head": set()}
    if not isinstance(artifacts, list):
        raise UnusableComparison(f"artifacts: observed {type(artifacts).__name__}, expected a list")
    artifact_ids = set()
    recorded = None
    for evidence in artifacts:
        try:
            def need(path, ok, expected, freshness=False):
                nonlocal stale
                value = _field(evidence, path, expected)
                if ok(value):
                    return value
                message = f"{path}: observed {_shown(value)}, expected {expected}"
                if not freshness:
                    raise UnusableComparison(message)
                stale = stale or message
                return value

            def equal(path, want, freshness=False):
                return need(path, lambda value: value == want, _shown(want), freshness)

            run_id = need("run.id", _positive, "a positive integer")
            equal("run.head_sha", head_sha)
            equal("run.status", "completed")
            equal("run.event", "pull_request")
            need("run.path", lambda value: isinstance(value, str) and value.split("@", 1)[0] == WORKFLOW,
                 _shown(WORKFLOW))
            equal("run.repository.full_name", repo)
            equal("run.head_repository.full_name", repo)
            artifact_id = need("artifact.id", _positive, "a positive integer")
            need("artifact.id", lambda value: value not in artifact_ids, "unique across shards")
            need("artifact.expired", lambda value: value is False, _shown(False))
            need("artifact.workflow_run.id", lambda value: type(value) is int and value == run_id, _shown(run_id))
            equal("artifact.workflow_run.head_sha", head_sha)
            need("report.schema_version", lambda value: type(value) is int and value == 1, _shown(1))
            equal("report.repository", repo)
            equal("report.base_sha", base_sha, freshness=True)
            equal("report.head_sha", head_sha)
            ci_excluded = need("report.excluded", _paths, "a list of test file paths")
            need("report.scope", lambda value: isinstance(value, dict) and value.keys() == expected_scope.keys(),
                 f"an object with keys {_shown(sorted(expected_scope))}")
            for key in expected_scope:
                if key != "excluded":
                    equal(f"report.scope.{key}", expected_scope[key])
            scope_excluded = need("report.scope.excluded", _paths, "a list of test file paths")
            equal("report.excluded", scope_excluded)
            if recorded is not None:
                need("report.excluded", lambda value: set(value) == recorded,
                     f"{_shown(sorted(recorded))} (exclusions of earlier shards)")
            recorded = set(ci_excluded)
            shards = need("report.shard_count", lambda value: type(value) is int and 1 <= value <= 256,
                          "an integer from 1 to 256")
            equal("report.shard_count", len(expected_shards))
            index = need("report.shard_index", lambda value: type(value) is int, "an integer")
            need("report.shard_index", lambda value: value in expected_shards and 0 <= value < shards,
                 f"one of {_shown(sorted(expected_shards))}")
            equal("artifact.name", f"acceptance-{index}")
            need("report.shard_index", lambda value: value not in reports, "unique across shards")
            if producer is not None:
                need("run.id", lambda value: value == producer, f"{producer} (producer of earlier shards)",
                     freshness=True)
            artifact_ids.add(artifact_id)
            seconds = need("report.duration_seconds",
                           lambda value: type(value) in (int, float) and isfinite(value) and value >= 0,
                           "a finite number of seconds, at least 0")
            producer = producer if producer is not None else run_id
            sides = {}
            for side in ("base", "head"):
                sides[side] = suite(_field(evidence, f"report.{side}", "an object"), f"report.{side}")
            ci_excluded = set(ci_excluded)
            for side in ("base", "head"):
                ids = sides[side][0]
                path = f"report.{side}.collected_ids"
                overlap = sorted(seen[side] & ids)
                if overlap:
                    raise UnusableComparison(f"{path}: observed {_shown(overlap[0])}, "
                                             "expected each test in only one shard")
                dropped = sorted(node for node in ids if node.split("::", 1)[0] in ci_excluded)
                if dropped:
                    raise UnusableComparison(f"{path}: observed {_shown(dropped[0])}, "
                                             f"expected no tests from report.excluded {_shown(sorted(ci_excluded))}")
                seen[side].update(ids)
            # Tests from CI's excluded files are already rejected above, so every
            # measured test stays protected even if it is excluded today.
            lost.update(sides["base"][1] - sides["head"][1])
            reports[index] = evidence["report"]
            duration = max(duration, seconds)
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError, OverflowError) as exc:
            raise UnusableComparison(f"Unusable acceptance shard: {exc}") from exc
    if not expected_shards or set(reports) != expected_shards:
        raise MissingComparison(f"missing acceptance shards: report.shard_index: observed {_shown(sorted(reports))}, "
                                f"expected {_shown(sorted(expected_shards)) if expected_shards else 'at least one shard'}")
    if stale:
        raise StaleComparison(f"Stale acceptance shard: {stale}")
    return sorted(lost), duration


def _refusal(exc):
    """The refused branch as one word; the exception's text stays detail and decides nothing."""
    if isinstance(exc, MissingComparison):
        return "missing"
    if isinstance(exc, StaleComparison):
        return "stale"
    if isinstance(exc, WorkflowChanged):
        return "workflow-changed"
    return "unavailable" if isinstance(exc, (TrackerError, GitError, OSError)) else "unusable"


def _note_evidence(ctx, n, base_sha, head, reason=None, detail=None):
    """Record a changed refusal; acceptance with no reason only closes one still open."""
    try:
        run = ctx.store.get_run(n)
        ctx.store.record_flow_change(
            "ci-evidence", issue=n, run_started_at=run.started_at if run is not None else None,
            only_if=None if reason else lambda latest: bool(latest and latest.get("refused")),
            refused=reason is not None, reason=reason, detail=detail, base_sha=base_sha, head_sha=head)
    except Exception:  # noqa: BLE001 - an observation never replaces the comparison's own result
        return


def reported_base(reports):
    """The one base every shard reports measuring, or None."""
    try:
        bases = {item["report"]["base_sha"] for item in reports}
    except (KeyError, TypeError):
        return None
    base = next(iter(bases), None) if len(bases) == 1 else None
    return base if isinstance(base, str) else None


def require_comparison(attempt, pr, head, infrastructure="", *, base_sha=None, status="pass", deadline=None,
                       later_base=None, older_base=None, wait=True, source=None):
    """Compare against ``base_sha``. CI measures whatever base the target had at
    the push, so a caller may pass ``later_base(sha)`` to accept a report on a
    later base it approves. ``older_base(sha)`` requests an existing base refresh
    by raising OlderMeasuredBase instead of waiting for an impossible report.
    A source supplies a tested merge train tip; validation stays here."""
    from foragent import ci_local, harness_guard

    n, ctx = attempt.n, attempt.ctx
    harness_guard.guard_comparison(attempt, head, pr)  # the comparison measures exactly this head
    if deadline is None:
        deadline = ctx.now().timestamp() + 1200
    if base_sha is None:
        base_sha = attempt.git.ref_sha(f"origin/{attempt.target_branch}")
    excluded = _baseline_exclusions(ctx, base_sha, attempt.log)
    scope = measurement_scope(ctx.config, ctx.repo_root, excluded)
    trusted_sha = (_trusted_sha(attempt.git, ctx.tracker, strict=True) if not wait and getattr(ctx.config, "merge_owner", False)
                   else _trusted_sha(attempt.git, ctx.tracker))
    evidence = ComparisonEvidence(base_sha, head, scope, trusted_sha, status, infrastructure)
    # pull_request executes the head's workflow. Its artifacts are usable only
    # when all acceptance workflows match the default branch exactly.
    workflow_error = None
    try:
        workflows_changed = changed_workflows(attempt.git, trusted_sha, head)
    except GitError as exc:
        workflows_changed = False
        workflow_error = str(exc)
    if workflows_changed:
        if source is not None:
            raise WorkflowChanged("prefix acceptance workflow differs from the default branch")
        _note_evidence(ctx, n, base_sha, head, "workflow-changed", workflows_changed.replace("\0", " ").strip())
        try:
            ci_local.fallback_comparison(attempt, base_sha)
            _note_evidence(ctx, n, base_sha, head)
            return evidence
        except ci_local.LocalComparisonUnavailable as exc:
            flag(ctx, n, Reason.ACCEPTANCE_BASELINE_UNAVAILABLE, "Local acceptance comparison unavailable", str(exc))
            raise ExecutorStopped("held", str(exc)) from exc
    if ci_protection(ctx) and not ci_evidence_target(ctx, attempt.target_branch):
        detail = (f"target {attempt.target_branch} cannot obtain foragent-acceptance CI evidence "
                  "(checks: ci, no local fallback)")
        _note_evidence(ctx, n, base_sha, head, "target-unsupported", detail)
        flag(ctx, n, Reason.ACCEPTANCE_BASELINE_UNAVAILABLE, "Acceptance comparison unavailable", detail)
        raise ExecutorStopped("held", detail)
    try:
        if workflow_error:
            raise UnusableComparison(workflow_error)
        # Measurement and artifact registration share ordinary CI's deadline.
        while True:
            measured_base, measured_head = base_sha, head
            if source is None:
                reports = ctx.tracker.pr_acceptance_reports(head)
            else:
                measured_base, measured_head, reports = source.measurement(attempt, head, base_sha)
            try:
                lost, duration = comparison(reports, ctx.repo, measured_base, measured_head, excluded,
                                           expected_scope=scope,
                                           expected_shards=range(ctx.config.ci_shards))
                if source is not None and any(not scope_covers(item["report"]["scope"], scope)
                        for item in reports):
                    raise StaleComparison("prefix measurement scope no longer covers the current one")
                recorded_reports = serialize_reports(reports)
                break
            except (MissingComparison, StaleComparison) as exc:
                reported = reported_base(reports) if isinstance(exc, StaleComparison) else None
                if (later_base is not None and reported is not None and reported != base_sha
                        and later_base(reported)):
                    base_sha = reported
                    excluded = _baseline_exclusions(ctx, base_sha, attempt.log)
                    scope = measurement_scope(ctx.config, ctx.repo_root, excluded)
                    evidence = ComparisonEvidence(base_sha, head, scope, trusted_sha, status, infrastructure)
                    continue
                if (older_base is not None and reported is not None and reported != base_sha
                        and older_base(reported)):
                    raise OlderMeasuredBase(reported)
                if not wait:
                    raise
                # Fresh CI replaces stale evidence; it never costs a worker turn.
                from foragent import runner

                _note_evidence(ctx, n, base_sha, head, _refusal(exc), f"{exc}\n{infrastructure}".strip())
                remaining = min(deadline - ctx.now().timestamp(), runner._remaining(n, ctx))
                if infrastructure or remaining <= 0:
                    raise
                from foragent.run_waits import _progress

                _progress(n, ctx)
                ctx.sleep(min(30, remaining))
    except (KeyError, TypeError, ValueError, RecursionError, OverflowError, AttributeError, TrackerError) as exc:
        if source is not None:
            raise  # A failed substitution leaves the ordinary comparison available.
        if isinstance(exc, OlderMeasuredBase) or (not wait and isinstance(exc, (MissingComparison, StaleComparison))):
            raise
        if isinstance(exc, TrackerError) and (exc.waits or (
                not wait and not isinstance(exc, RejectedEvidence))):
            raise
        detail = f"{exc}\n{infrastructure}".strip()
        _note_evidence(ctx, n, base_sha, head, _refusal(exc), detail)
        # A local measurement is no substitute for current CI evidence.
        if ctx.config.checks_fallback == "local" and not isinstance(exc, StaleComparison):
            try:
                ci_local.fallback_comparison(attempt, base_sha)
                _note_evidence(ctx, n, base_sha, head)
                return evidence
            except ci_local.LocalComparisonUnavailable as fallback:
                detail += f"\n{fallback}"
        flag(ctx, n, Reason.ACCEPTANCE_BASELINE_UNAVAILABLE, "Acceptance comparison unavailable", detail)
        raise ExecutorStopped("held", detail) from exc
    ctx.store.update_run(n, comparison={"source": "ci", "duration_seconds": duration})
    if not infrastructure:
        _note_evidence(ctx, n, base_sha, head)  # A regression below fails the work, not this evidence.
    if lost:
        from foragent import run_publish

        run_publish._record_failure(n, ctx, pr, head)  # CI measured this head losing tests: a code failure.
        raise AttemptFailure("Acceptance baseline regression (previously passing tests):\n" + "\n".join(lost),
                             attempt.target_branch)
    if infrastructure:
        _note_evidence(ctx, n, base_sha, head, "infrastructure", infrastructure)
        if ctx.config.checks_fallback == "local":
            try:
                ci_local.fallback_comparison(attempt, base_sha)
                _note_evidence(ctx, n, base_sha, head)
                return evidence
            except ci_local.LocalComparisonUnavailable as exc:
                infrastructure += f"\n{exc}"
        flag(ctx, n, Reason.CI_NOT_STARTED, "CI jobs did not start", infrastructure)
        raise ExecutorStopped("held", infrastructure)
    return ComparisonEvidence(base_sha, head, scope, trusted_sha, status, infrastructure,
                              recorded_reports, reports_by_head=(
                                  {measured_head: recorded_reports}
                                  if source is not None else None))


def review_checks(n, ctx, git, target_branch, *, files=None, excluded=None):
    """Rechecking an existing issue PR uses the same CI and machine evidence."""
    from types import SimpleNamespace
    from foragent import harness_guard, runner
    from foragent.ci_local import capture_local_only, check, verify_local_only_units
    from foragent.run_checks import _related_unit_tests, run_related_check, trusts_ci
    from foragent.run_waits import in_phase
    import shlex

    base = f"origin/{target_branch}"
    command = ctx.config.test_command or next(iter(runner.detect_test_commands(git.root)), None)
    if excluded is None:
        excluded = _baseline_exclusions(ctx, git.ref_sha(base), ctx.store.log_path(n))
    try:
        baseline = capture_local_only(n, ctx, git, command, git.ref_sha(base), excluded)
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
            harness_guard.guard_comparison(attempt, git.head_sha(), files=files)
            checks.append(check(n, ctx, f"{baseline.command} {shlex.join(baseline.node_ids)}", git.root))
        if own:
            # Related tests are a recorded check of their own, as in an implementation's verification.
            checks.append(run_related_check(n, ctx, git.root, baseline.command, own, len(changed),
                                            max(1, runner._remaining(n, ctx))))
    for result in checks:
        if result.exit_code != 0 or result.timed_out:
            raise AttemptFailure(f"{result.command}\n{result.output}", target_branch)
    return attempt
