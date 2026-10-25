"""Machine-only acceptance baselines and on-demand local fallback."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from foragent.run_attempt import AttemptFailure
from foragent.run_checks import _passing_acceptance_ids, _pytest_command, junit_node_id
from foragent.run_waits import _progress, _test_slot


def report_ids(report, result, measurement):
    """A missing/partial/session-error JUnit report can never prove a baseline."""
    from foragent.ci_mode import suite

    tree = ET.parse(report)
    cases = list(tree.iter("testcase"))
    ids = {junit_node_id(case) for case in cases}
    collected, passing = suite(measurement)
    expected = int(any(case.find(tag) is not None for case in cases for tag in ("failure", "error")))
    if not cases and result.exit_code == 5:
        expected = 5
    if (tree.getroot().tag not in {"testsuite", "testsuites"} or None in ids or result.timed_out
            or result.exit_code != expected or tree.find(".//error[@message='collection failure']") is not None
            or measurement.get("exit_code") != result.exit_code or ids != collected
            or set(_passing_acceptance_ids(report)) != passing):
        raise ValueError("incomplete pytest report")
    return tuple(sorted(ids)), _passing_acceptance_ids(report)


def check(n, ctx, command, root):
    from foragent import runner
    return runner._check(command, root, ctx.store.log_path(n), max(1, runner._remaining(n, ctx)),
                         progress=lambda: _progress(n, ctx), slot=_test_slot(n, ctx))


@contextmanager
def base_checkout(ctx, git, base_sha):
    # A fresh issue worktree already IS the exact base: measure before the worker
    # changes it. Resumed work must use an independent checkout of that same SHA.
    if git.head_sha() == base_sha and not git.has_staged_changes() and not git.uncommitted_paths(["."]):
        yield git.root
        return
    with tempfile.TemporaryDirectory(prefix="local-only-", dir=ctx.store.root) as directory:
        root = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(root, base_sha)
        try:
            yield root
        finally:
            ctx.git.remove_worktree(root)


def capture_local_only(n, ctx, git, test_command, base_sha, excluded):
    from foragent.run_baseline import AcceptanceBaseline, _validate_baseline_checkout

    command = _pytest_command(test_command) if test_command else None
    if command is None:
        raise AttemptFailure("local_only checks require a supported pytest command")
    report = ctx.store.root / "runs" / f"issue-{n}-local-only.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    with base_checkout(ctx, git, base_sha) as root:
        if not (root / ctx.config.acceptance_dir).is_dir():
            return AcceptanceBaseline(command, (), excluded=tuple(excluded))
        report.unlink(missing_ok=True)
        selection = f"{command} {shlex.quote(ctx.config.acceptance_dir)} -m local_only"
        selection += "".join(f" {shlex.quote('--ignore=' + name)}" for name in excluded)
        with tempfile.TemporaryDirectory(prefix="measurement-", dir=report.parent) as directory:
            plugin = Path(directory)
            shutil.copyfile(Path(__file__).with_name("ci_pytest.py"), plugin / "fa_local_measurement.py")
            evidence = plugin / "results.json"
            pythonpath = os.pathsep.join(filter(None, [directory, os.environ.get("PYTHONPATH")]))
            environment = (f"PYTHONPATH={shlex.quote(pythonpath)} FA_REPORT={shlex.quote(str(evidence))} "
                           "FA_SHARD=0 FA_SHARDS=1")
            result = check(n, ctx, f"{environment} {selection} -p fa_local_measurement "
                           f"-o junit_family=legacy --junit-prefix= --junitxml={shlex.quote(str(report))}", root)
            _validate_baseline_checkout(git.at(root), base_sha)
            try:
                collected, passing = report_ids(report, result, json.loads(evidence.read_text()))
            except (OSError, ValueError, ET.ParseError) as exc:
                raise AttemptFailure(f"local_only acceptance baseline unavailable: {exc}") from exc
        return AcceptanceBaseline(command, passing, collected, tuple(sorted(set(collected) - set(passing))), tuple(excluded))


def verify_local_only_units(attempt, command):
    from dataclasses import replace
    from foragent import runner
    from foragent.run_checks import CheckResult, _append

    acceptance = Path(attempt.ctx.config.acceptance_dir)
    if not any(not path.relative_to(attempt.worktree).is_relative_to(acceptance)
               for path in attempt.worktree.rglob("*.py")):
        return None
    selection = f"{command} -m local_only {shlex.quote('--ignore=' + str(acceptance))}"
    collection = f"{selection} --collect-only -q"

    def collect():
        with _test_slot(attempt.n, attempt.ctx):
            try:
                result = subprocess.run(
                    ["/bin/sh", "-c", collection], cwd=attempt.worktree, check=False,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    timeout=max(1, runner._remaining(attempt.n, attempt.ctx)),
                )
            except subprocess.TimeoutExpired as exc:
                return CheckResult(collection, None, True, str(exc))
        return CheckResult(collection, result.returncode, False, result.stdout)

    collected = runner.poll_executor(collect, lambda: _progress(attempt.n, attempt.ctx))
    _append(attempt.ctx.store.log_path(attempt.n),
            f"Local-only unit collection exit_code={collected.exit_code}\n{collected.output}")
    if collected.exit_code == 5 and not collected.timed_out:
        return None
    if collected.exit_code != 0 or collected.timed_out:
        return collected

    result = check(attempt.n, attempt.ctx, selection, attempt.worktree)
    # pytest's no-tests-selected status is valid for this optional marker set.
    return replace(result, exit_code=0) if result.exit_code == 5 and not result.timed_out else result


class LocalComparisonUnavailable(ValueError):
    pass


def fallback_comparison(attempt, base_sha):
    from foragent.run_baseline import _capture_acceptance_baseline

    n, ctx = attempt.n, attempt.ctx
    started = ctx.monotonic()
    head = attempt.git.head_sha()
    try:
        baseline = _capture_acceptance_baseline(n, ctx, ctx.config.test_command or attempt.baseline.command, base_sha)
    except AttemptFailure as exc:
        raise LocalComparisonUnavailable(str(exc)) from exc
    if baseline is None:
        raise LocalComparisonUnavailable("fallback acceptance baseline unavailable")
    result = None
    if baseline.node_ids:
        result = check(n, ctx, f"{baseline.command} {shlex.join(baseline.node_ids)}", attempt.worktree)
    if (attempt.git.head_sha() != head
            or attempt.git._call(["status", "--porcelain=v1", "--untracked-files=no"])):
        # The normal implementation retry stages and rechecks rewrites. Retain
        # the full local baseline so that verification covers the rewritten tree.
        attempt.baseline = baseline
        raise AttemptFailure("Acceptance comparison modified tracked files; reverify with the local baseline.",
                             attempt.target_branch)
    if result is not None and (result.exit_code != 0 or result.timed_out):
        raise AttemptFailure(f"Acceptance baseline regression:\n{result.command}\n{result.output}", attempt.target_branch)
    ctx.store.update_run(n, comparison={"source": "local", "duration_seconds": max(0, ctx.monotonic() - started)})
