"""Machine-only acceptance baselines and on-demand local fallback."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
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
    # Per test file at ``head``: its collected ids and its local_only ids (None: not collectable).
    layout: dict = field(default_factory=dict, repr=False)
    # Only a fully validated, trusted batch comparison can credit locked paths.
    locked_ci: frozenset[str] = frozenset()
    pythonpath: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def read(cls, ctx, head, *, published=True):
        evidence = _ci_evidence(ctx, head, published=published)
        return cls(head, evidence[1] if evidence else None, run_id=evidence[0] if evidence else None)

    def passed(self) -> set[str]:
        return set(self.ci or ()) | self.locked_ci | self.ran


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


_NODE_LINE = re.compile(r"^(.+?\.py)::\S")
_PASSED_LINE = re.compile(r"^PASSED (\S.*?\.py::.+)$")


def _collect_ids(n, ctx, root, invocation, files, extra, log, *, probe=None):
    """Node ids foragent's own pytest invocation collects in ``files``; [] when none; None when not clean."""
    from foragent import runner

    selection = [*sorted(files), *([str(probe)] if probe is not None else [])]
    command = f"{invocation} --collect-only -q {extra}{shlex.join(selection)}"
    log.unlink(missing_ok=True)
    result = runner._check(command, root, log, max(1, runner._remaining(n, ctx)), progress=lambda: _progress(n, ctx),
                           slot=_test_slot(n, ctx), clock=ctx.monotonic)
    if result.exit_code == 5 and not result.timed_out:
        return []
    if result.exit_code != 0 or result.timed_out:
        return None
    text = log.read_text(encoding="utf-8").split("\nCheck exit_code=", 1)[0].split("\n", 2)[-1]
    ids = [row for row in text.splitlines() if _NODE_LINE.match(row) and _NODE_LINE.match(row).group(1) in files]
    return ids or None


def _deselections(n, ctx, root, skips, locked, directory):
    """CI-passed or already passed ids; locked paths require trusted batch evidence.

    Collection preserves the existing conservative prefix selection for ordinary
    CI and identifies local_only tests. Trusted locked ids also receive exact
    matching in the plugin, so their uncredited siblings can still run.
    """
    from foragent import runner

    def is_locked(path):
        return any(path == item or path.startswith(item.rstrip("/") + "/") for item in locked)

    ci = {node for node in (skips.ci or frozenset()) | skips.locked_ci
          if "::" in node and (node in skips.locked_ci or not is_locked(node.split("::", 1)[0]))}
    ran = {node for node in skips.ran if "::" in node}
    files = {node.split("::", 1)[0] for node in ci | ran}
    missing = {path for path in files - skips.layout.keys() if (root / path).is_file()}
    if missing:
        test_command = ctx.config.test_command or next(iter(runner.detect_test_commands(root)), None)
        invocation = _pytest_command(test_command) if test_command else None
        log = Path(directory) / "collect.log"
        # Ask pytest for its configured paths during the existing collection.
        # The plugin must remain importable with Python -I/-E or inline PYTHONPATH,
        # while retaining the project's own pythonpath setting and plugins.
        settings = Path(directory) / "pythonpath.json"
        # Keep the probe beside an existing selection. An out-of-tree selector
        # changes pytest's common ancestor and can make it discover another config.
        parent = (root / min(missing)).parent
        ids = None
        if invocation and parent.resolve().is_relative_to(root.resolve()):
            with tempfile.TemporaryDirectory(prefix=".owner-collect-", dir=parent) as probes:
                probe = Path(probes) / ("test_" + Path(probes).name.replace("-", "_").replace(".", "_") + ".py")
                probe.write_text(
                    "import json\nfrom pathlib import Path\n"
                    "def pytest_generate_tests(metafunc):\n"
                    f"    Path({str(settings)!r}).write_text(json.dumps([str(p) for p in metafunc.config.getini('pythonpath')]))\n"
                    "def test_config():\n    pass\ntest_config.__test__ = True\n", encoding="utf-8")
                ids = _collect_ids(n, ctx, root, invocation, missing, "", log, probe=probe)
        if ids is not None:
            try:
                skips.pythonpath = tuple(json.loads(settings.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                ids = None
        local = _collect_ids(n, ctx, root, invocation, missing, "-m local_only ", log) if ids else None
        for path in missing:
            skips.layout[path] = None if ids is None or local is None else (
                [node for node in ids if node.split("::", 1)[0] == path],
                {node for node in local if node.split("::", 1)[0] == path})
    out = []
    for path in sorted(files):
        if (layout := skips.layout.get(path)) is None:
            continue  # Not collectable here: nothing in this file is left out.
        ids, local = layout
        leave = {node for node in ids if node in ran or (node in ci and node not in local)}
        out += [node for node in ids if node in leave
                and not any(other != node and other.startswith(node) and other not in leave for other in ids)]
    return out


@contextmanager
def skipping(n, ctx, command, skips, locked=(), label="Done when", *, root, owned=False):
    """Yield ``command`` so that every pytest it starts leaves out what ``skips`` covers (D124).

    Approved command text is unchanged. Each pytest receives options through
    PYTEST_ADDOPTS and a temporary plugin: a session whose remaining tests this
    skip removed entirely passes its own shell segment, without ever replaying
    the command. Real failures keep their status, and so does exit 5 from -k,
    -m, another plugin's deselection or a path with no tests. Trusted locked ids
    are deselected exactly, including ids that prefix an uncredited sibling.
    ``owned`` normalized invocations clear PYTEST_ADDOPTS, so receive the options
    as arguments instead. PASSED summary lines record what this pass ran.
    """
    from foragent.run_checks import _append, verification_environment

    if skips is None:
        yield command
        return
    runs = ctx.store.root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    log = ctx.store.log_path(n)
    with tempfile.TemporaryDirectory(prefix="owner-skip-", dir=runs) as directory:
        out = _deselections(n, ctx, root, skips, locked, directory)
        arguments = Path(directory) / "arguments.txt"
        words = ["-rp"]
        options = []
        environment = {}
        if out or skips.locked_ci:
            module = "fa_" + Path(directory).name.replace("-", "_")
            paths = [directory, *skips.pythonpath]
            words += ["-o", "pythonpath=" + shlex.join(paths)]
            # -p must be visible in addopts/argv, not hidden inside the argument
            # file: pytest's early plugin loader does not expand @files itself.
            options += ["-p", module]
            # CI ids are repository-relative even when pytest discovers an ancestor config/root.
            (Path(directory) / f"{module}.py").write_text(
                "from pathlib import Path\nimport pytest\n"
                "_emptied = pytest.StashKey[bool]()\n"
                f"_original = [Path(p) for p in {list(skips.pythonpath)!r}]\n"
                f"_bootstrap = [Path(p) for p in {paths!r}]\n"
                f"_trusted = {set(skips.locked_ci)!r}\n"
                f"_selected = {set(out)!r}\n"
                f"_root = Path({str(root.resolve())!r})\n"
                "def pytest_addoption(parser, pluginmanager):\n"
                "    config = pluginmanager.get_plugin('pytestconfig')\n"
                "    if config.getini('pythonpath') == _bootstrap:\n"
                "        config._inicache['pythonpath'] = _original\n"
                "def _node_id(item):\n"
                "    try:\n"
                "        path = item.path.relative_to(_root).as_posix()\n"
                "    except ValueError:\n"
                "        return None\n"
                "    _, separator, tail = item.nodeid.partition('::')\n"
                "    return path + '::' + tail if separator else None\n"
                # Collection finish follows every pytest_collection_modifyitems
                # implementation and wrapper (-k, -m, --lf/--lfnf, --deselect, conftest
                # and other plugins), so ``session.items`` is exactly what would
                # otherwise run. Only when this skip removes every one of them is the
                # session marked, so an empty run caused by anything else keeps exit 5.
                # tryfirst: the terminal reporter then counts these as deselected.
                "@pytest.hookimpl(tryfirst=True)\n"
                "def pytest_collection_finish(session):\n"
                "    items = session.items\n"
                "    leave = [item for item in items if _node_id(item) in _selected\n"
                "             or (_node_id(item) in _trusted and item.get_closest_marker('local_only') is None)]\n"
                "    if leave:\n"
                "        items[:] = [item for item in items if item not in leave]\n"
                "        session.config.hook.pytest_deselected(items=leave)\n"
                "        session.stash[_emptied] = not items\n"
                "@pytest.hookimpl(trylast=True)\n"
                "def pytest_sessionfinish(session, exitstatus):\n"
                "    if (exitstatus == 5 and session.exitstatus == 5 and session.testscollected == 0\n"
                "            and session.stash.get(_emptied, False)):\n"
                "        session.exitstatus = 0\n", encoding="utf-8")
            environment["PYTHONPATH"] = os.pathsep.join(filter(None, [directory, os.environ.get("PYTHONPATH")]))
        arguments.write_text("".join(f"{word}\n" for word in words), encoding="utf-8")
        options.append(f"@{arguments}")
        _append(log, f"{label}: leaving out {len(out)} tests CI passed on {skips.head}")
        offset = log.stat().st_size if log.exists() else 0
        # pytest splits PYTEST_ADDOPTS as shell words; preserve the @path's
        # quoting in the environment value even when the state folder has spaces.
        option = shlex.join(options)
        environment["PYTEST_ADDOPTS"] = " ".join(filter(None, [os.environ.get("PYTEST_ADDOPTS"), option]))
        with verification_environment(environment):
            yield f"{command} {option}" if owned else command
        try:
            with log.open(encoding="utf-8", errors="replace") as stream:
                stream.seek(offset)
                skips.ran.update(match.group(1) for row in stream if (match := _PASSED_LINE.match(row.rstrip("\n"))))
        except OSError:
            pass


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

    with skipping(attempt.n, attempt.ctx, selection, skips, label="Local-only units",
                  root=attempt.worktree, owned=True) as selected:
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
                          label="Acceptance comparison", root=attempt.worktree, owned=True) as selected:
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
