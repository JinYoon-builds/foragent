"""Machine-only acceptance baselines and on-demand local fallback."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import xml.etree.ElementTree as ET

from foragent import cuts
from foragent.run_attempt import AttemptFailure
from foragent.run_checks import _passing_acceptance_ids, _pytest_command, junit_node_id
from foragent.run_waits import _phase, _progress, _test_slot, in_phase, phase_identity, restore_phase


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
                         progress=lambda: _progress(n, ctx), slot=_test_slot(n, ctx),
                         full_text_dir=cuts.issue_dir(ctx.store.root, n))


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
    command = _pytest_command(test_command) if test_command else None
    if command is None:
        raise AttemptFailure("local_only checks require a supported pytest command")
    # Measuring the base is the baseline phase; a second measurement of this base repeats it.
    previous = phase_identity(ctx.store.get_run(n))
    _phase(n, ctx, "baseline")
    ctx.store.update_run(n, phase_work_id=f"baseline-local:{base_sha}")
    try:
        return _capture_local_only(n, ctx, git, command, base_sha, excluded)
    finally:
        restore_phase(n, ctx, previous)


def _capture_local_only(n, ctx, git, command, base_sha, excluded):
    from foragent.run_baseline import AcceptanceBaseline, _validate_baseline_checkout

    report = ctx.store.root / "runs" / f"issue-{n}-local-only.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    with base_checkout(ctx, git, base_sha) as root:
        if not (root / ctx.config.acceptance_dir).is_dir():
            return AcceptanceBaseline(command, (), excluded=tuple(excluded), base_sha=base_sha)
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
        return AcceptanceBaseline(command, passing, collected, tuple(sorted(set(collected) - set(passing))), tuple(excluded), base_sha)


def _uses_local_only(root):
    # This is only a cheap presence check; pytest still decides which tests are marked.
    paths = [*(root / "tests").rglob("*.py"), *root.glob("test_*.py"), *root.glob("*_test.py"),
             root / "conftest.py", root / "pytest.ini", root / ".pytest.ini", root / "pyproject.toml",
             root / "setup.cfg", root / "tox.ini", root / "pytest.toml", root / ".pytest.toml"]
    for path in paths:
        try:
            if b"local_only" in path.read_bytes():
                return True
        except OSError:
            continue
    return False


def verify_local_only_units(attempt, command):
    from dataclasses import replace
    from foragent.run_checks import _append

    acceptance = Path(attempt.ctx.config.acceptance_dir)
    if not _uses_local_only(attempt.worktree):
        return None
    selection = f"{command} -m local_only {shlex.quote('--ignore=' + str(acceptance))}"
    collection = f"{selection} --collect-only -q"

    try:
        collected = check(attempt.n, attempt.ctx, collection, attempt.worktree)
    except OSError as exc:
        _append(attempt.ctx.store.log_path(attempt.n),
                f"Local-only unit collection unavailable: {exc}; continuing with related tests")
        return None
    _append(attempt.ctx.store.log_path(attempt.n),
            f"Local-only unit collection exit_code={collected.exit_code}, timed_out={collected.timed_out}\n"
            f"{collected.output}")
    if collected.exit_code == 5 and not collected.timed_out:
        return None
    if collected.exit_code != 0 or collected.timed_out:
        _append(attempt.ctx.store.log_path(attempt.n),
                "Local-only unit discovery unavailable; continuing with related tests")
        return None

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
        # Running the acceptance comparison on the head is checking time, not CI waiting.
        with in_phase(n, ctx, "checking"):
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
