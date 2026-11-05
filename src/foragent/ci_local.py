"""Machine-only acceptance baselines and on-demand local fallback."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import tempfile
import xml.etree.ElementTree as ET

from foragent import cuts
from foragent.run_attempt import AttemptFailure
from foragent.run_checks import _passing_acceptance_ids, _pytest_command, junit_node_id
from foragent.run_waits import _phase, _progress, _test_slot, in_phase, phase_identity, restore_phase


@dataclass
class PassSkips:
    """D124 evidence for one owner pass on one checked commit.

    ``ci`` holds the tests CI passed on exactly ``head`` (None when no source is
    readable: then nothing is skipped from it). ``ran`` collects the tests this
    pass already passed on the same commit, so a later step does not repeat them.
    """

    head: str
    ci: frozenset[str] | None
    ran: set[str] = field(default_factory=set)
    run_id: object = None  # The exact-head CI run that passed ``ci``.

    @classmethod
    def read(cls, ctx, head, *, published=True):
        evidence = _ci_evidence(ctx, head, published=published)
        return cls(head, evidence[1] if evidence else None, run_id=evidence[0] if evidence else None)

    def passed(self) -> set[str]:
        return set(self.ci or ()) | self.ran


def ci_passing_set(ctx, head, *, published=True):
    """Tests CI passed on exactly ``head``; None (skip nothing) without its unit receipt."""
    evidence = _ci_evidence(ctx, head, published=published)
    return evidence[1] if evidence else None


def _ci_evidence(ctx, head, *, published=True):
    """Tests CI passed on exactly ``head``: the unit receipt joined with the acceptance reports.

    None (skip nothing) without the exact-head unit receipt; unreadable reports add nothing.
    The acceptance measurement runs only on a pushed PR head, so an unpublished
    commit (``published=False``) has no reports to read.
    """
    from foragent.ci_mode import UnusableComparison, suite
    from foragent.tracker import TrackerError

    found = set()
    try:
        unit = ctx.tracker.pr_unit_passed_tests(head)
    except TrackerError as exc:
        if exc.waits:
            raise
        unit = None
    if unit is None:
        # The exact-head receipt is CI's proof that it passed this commit; without it nothing is credited.
        return None
    found.update(unit[1])
    if not published:
        return unit[0], frozenset(found)
    try:
        reports = ctx.tracker.pr_acceptance_reports(head)
        acceptance = set()
        for item in reports:
            report = item["report"]
            if (report["head_sha"] != head or item["run"]["head_sha"] != head
                    or report["repository"] != ctx.repo):
                raise ValueError("acceptance report measured another commit")
            acceptance.update(suite(report["head"], "report.head")[1])
    except TrackerError as exc:
        if exc.waits:
            raise
    except (KeyError, TypeError, ValueError, UnusableComparison):
        pass
    else:
        found.update(acceptance)
    return unit[0], frozenset(found)


SKIP_PLUGIN = """\
import json
import os
import pytest

# Tests may start pytest themselves; those inner sessions must not load this plugin.
_names = [name for name in os.environ.get("PYTEST_PLUGINS", "").split(",") if name.strip() not in ("", {name!r})]
if _names:
    os.environ["PYTEST_PLUGINS"] = ",".join(_names)
else:
    os.environ.pop("PYTEST_PLUGINS", None)
_paths = [path for path in os.environ.get("PYTHONPATH", "").split(os.pathsep) if path and path != {directory!r}]
if _paths:
    os.environ["PYTHONPATH"] = os.pathsep.join(_paths)
else:
    os.environ.pop("PYTHONPATH", None)

with open({listing!r}, encoding="utf-8") as stream:
    _skip = json.load(stream)
_ci, _ran, _locked = set(_skip["ci"]), set(_skip["ran"]), tuple(_skip["locked"])
try:  # Earlier lines of the same Done when block already passed these on this commit.
    with open({ran!r}, encoding="utf-8") as stream:
        for line in stream:
            _ran.update(json.loads(line)["passed"])
except FileNotFoundError:
    pass
_seen, _failed, _counts = set(), set(), {{"collected": 0, "skipped": 0}}


def _keep(item):
    if item.nodeid in _ran:
        return False
    if item.nodeid not in _ci:
        return True
    path = item.nodeid.split("::", 1)[0]
    if any(path == locked or path.startswith(locked.rstrip("/") + "/") for locked in _locked):
        return True
    return item.get_closest_marker("local_only") is not None


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    result = yield
    deselected = [item for item in items if not _keep(item)]
    _counts.update(collected=len(items), skipped=len(deselected))
    if deselected:
        items[:] = [item for item in items if _keep(item)]
        config.hook.pytest_deselected(items=deselected)
    return result


def pytest_runtest_logreport(report):
    if report.passed and report.when == "call":
        _seen.add(report.nodeid)
    elif report.failed:
        _failed.add(report.nodeid)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 5 and _counts["skipped"] and _counts["skipped"] == _counts["collected"]:
        session.exitstatus = 0  # Every selected test already passed on this commit.
    with open({ran!r}, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({{"passed": sorted(_seen - _failed), **_counts}}) + "\\n")
"""


@contextmanager
def skipping(n, ctx, command, skips, locked=(), label="Done when"):
    """Yield ``command`` so that every pytest it starts leaves out what ``skips`` covers (D124).

    The command text is never rewritten: one environment line in front of it
    activates the plugin through PYTEST_PLUGINS, so quoting, globs, expansions,
    comments, ``--`` and line continuations keep their shell meaning. Locked
    paths and ``local_only`` tests always run despite CI evidence; tests that
    passed earlier in this pass on the same commit are left out as well.
    """
    from foragent.run_checks import _append

    # A command that sets these itself would replace the activation and cannot load the plugin.
    if skips is None or "PYTHONPATH" in command or "PYTEST_PLUGINS" in command:
        yield command
        return
    runs = ctx.store.root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="owner-skip-", dir=runs) as directory:
        listing, ran = Path(directory) / "skip.json", Path(directory) / "ran.jsonl"
        listing.write_text(json.dumps({"ci": sorted(skips.ci or ()), "ran": sorted(skips.ran),
                                       "locked": sorted(locked)}), encoding="utf-8")
        plugin = f"_foragent_owner_skip_{secrets.token_hex(8)}"
        (Path(directory) / f"{plugin}.py").write_text(
            SKIP_PLUGIN.format(listing=str(listing), ran=str(ran), name=plugin, directory=directory),
            encoding="utf-8")
        activation = (f"PYTEST_PLUGINS={plugin}${{PYTEST_PLUGINS:+,$PYTEST_PLUGINS}}; "
                      f"PYTHONPATH={shlex.quote(directory)}${{PYTHONPATH:+:$PYTHONPATH}}; "
                      "export PYTEST_PLUGINS PYTHONPATH\n")
        yield activation + command
        try:
            outcomes = [json.loads(line) for line in ran.read_text(encoding="utf-8").splitlines()]
        except FileNotFoundError:
            outcomes = []  # No pytest session ran (a command without pytest).
        except (OSError, ValueError) as exc:
            _append(ctx.store.log_path(n), f"{label}: no skip record for {skips.head}: {exc}")
            return
        try:
            skipped = sum(outcome["skipped"] for outcome in outcomes)
            collected = sum(outcome["collected"] for outcome in outcomes)
            for outcome in outcomes:
                skips.ran.update(outcome["passed"])
        except (KeyError, TypeError):
            _append(ctx.store.log_path(n), f"{label}: no skip record for {skips.head}")
            return
        _append(ctx.store.log_path(n), f"{label}: skipped {skipped} of {collected} "
                f"using CI evidence for {skips.head}")


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
                         progress=lambda: _progress(n, ctx), slot=_test_slot(n, ctx), clock=ctx.monotonic,
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
    completed = False
    try:
        result = _capture_local_only(n, ctx, git, command, base_sha, excluded)
        completed = True
        return result
    finally:
        restore_phase(n, ctx, previous, resume=completed)


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
        return AcceptanceBaseline(command, passing, collected, tuple(sorted(set(collected) - set(passing))),
                                  tuple(excluded), base_sha, duration_seconds=result.seconds)


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


def verify_local_only_units(attempt, command, *, skips=None):
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

    with skipping(attempt.n, attempt.ctx, selection, skips, label="Local-only units") as selected:
        result = check(attempt.n, attempt.ctx, selected, attempt.worktree)
    result = replace(result, command=selection)
    # pytest's no-tests-selected status is valid for this optional marker set.
    return replace(result, exit_code=0) if result.exit_code == 5 and not result.timed_out else result


class LocalComparisonUnavailable(ValueError):
    pass


def fallback_comparison(attempt, base_sha, *, skips=None):
    from foragent import harness_guard, runner
    from foragent.run_baseline import _capture_acceptance_baseline

    n, ctx = attempt.n, attempt.ctx
    started = ctx.monotonic()
    if getattr(ctx.config, "merge_owner", False):
        command = (ctx.config.test_command or getattr(attempt.baseline, "command", None)
                   or next(iter(runner.detect_test_commands(attempt.worktree)), None))
        if command is None:
            raise LocalComparisonUnavailable("fallback acceptance test command unavailable")
    else:
        command = ctx.config.test_command or attempt.baseline.command
    try:
        baseline = _capture_acceptance_baseline(n, ctx, command, base_sha)
    except AttemptFailure as exc:
        raise LocalComparisonUnavailable(str(exc)) from exc
    if baseline is None:
        raise LocalComparisonUnavailable("fallback acceptance baseline unavailable")
    # This checkout's commit is what is measured (not the PR head the caller named): guard that,
    # and accept the result only if the checkout is still exactly that commit afterwards.
    head = attempt.git.head_sha()
    harness_guard.guard_comparison(attempt, head)
    result = None
    if baseline.node_ids:
        # Running the acceptance comparison on the head is checking time, not CI waiting.
        with in_phase(n, ctx, "checking"):
            command = f"{baseline.command} {shlex.join(baseline.node_ids)}"
            with skipping(n, ctx, command, skips if skips is not None and skips.head == head else None,
                          label="Acceptance comparison") as selected:
                result = check(n, ctx, selected, attempt.worktree)
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
