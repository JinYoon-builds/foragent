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


# pytest options that take the next word as their value (others are flags or carry `=value`).
_VALUE_OPTIONS = {
    "-k", "-m", "-p", "-o", "-c", "-W", "-r", "-n", "--deselect", "--ignore", "--ignore-glob", "--maxfail",
    "--junitxml", "--junit-xml", "--verbosity", "--color", "--config-file", "--override-ini", "--rootdir",
    "--confcutdir", "--basetemp", "--import-mode", "--tb", "--capture", "--code-highlight", "--junit-prefix",
    "--durations", "--durations-min", "--pythonwarnings", "--dist", "--numprocesses", "--log-level",
    "--log-cli-level", "--log-file", "--log-file-level", "--log-format", "--log-date-format", "--timeout",
    "--cov", "--cov-report", "--cov-config", "--cov-fail-under", "--pyargs", "--deselect",
}
_VERBOSITY = {"-q", "-v", "--quiet", "--verbose"}
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=.*", re.S)
_NODE_LINE = re.compile(r"^([\w./-]+\.py)::\S")


@dataclass
class _PlainPytest:
    """One Done when line that is a single plain pytest command, split into what pytest reads."""

    assignments: list[str]
    launcher: list[str]  # Everything up to and including the pytest word.
    options: list[str]
    selection: list[str]

    def shell(self, options, selection):
        prefix = [f"{name}={shlex.quote(value)}" for name, value in
                  (word.split("=", 1) for word in self.assignments)]
        return " ".join([*prefix, shlex.join([*self.launcher, *options, *selection])])


def _certain_text(line):
    """The line without a trailing comment when the shell reads its words literally; otherwise None.

    Outside quotes: expansions, escapes, globs, braces, tildes and operators are not
    certain, and a ``#`` must start a word (a comment to the end of the line).
    Inside double quotes only ``$``, backquote and backslash expand. Single quotes are literal.
    """
    quote = None
    for i, char in enumerate(line):
        if quote == "'":
            quote = None if char == "'" else quote
        elif quote == '"':
            if char in "$`\\":
                return None
            quote = None if char == '"' else quote
        elif char in "'\"":
            quote = char
        elif char == "#":
            if i and not line[i - 1].isspace():
                return None
            return line[:i] if line[:i].strip() else None
        elif char in "$`\\*?[]{}~;&|<>()!":
            return None
    return None if quote else line


def _plain_pytest(line):
    """The line's pytest parts, or None for any form whose shell meaning is not certain.

    Certain means: no expansions, globs, quoting-sensitive comments, escapes,
    operators, redirections or unknown options that may take a value. Anything
    else runs exactly as written, without skipping.
    """
    line = _certain_text(line)
    if line is None:
        return None
    words = shlex.split(line)
    if not words or any(any(char in "();<>|&!" for char in word) for word in words):
        return None
    index = next((i for i, word in enumerate(words) if Path(word).name in {"pytest", "py.test"}), None)
    if index is None:
        return None
    assignments = []
    while assignments.__len__() < index and _ASSIGNMENT.fullmatch(words[len(assignments)]):
        assignments.append(words[len(assignments)])
    options, selection, rest = [], [], iter(words[index + 1:])
    for word in rest:
        if word == "--":
            selection.extend(rest)
            break
        if not word.startswith("-"):
            selection.append(word)
            continue
        options.append(word)
        if word in _VALUE_OPTIONS:
            value = next(rest, None)
            if value is None:
                return None
            options.append(value)
        elif word.startswith("--") and "=" not in word:
            known_flag = word in {"--quiet", "--verbose", "--exitfirst", "--strict-markers", "--strict-config",
                                  "--lf", "--last-failed", "--ff", "--failed-first", "--sw", "--stepwise",
                                  "--disable-warnings", "--showlocals", "--runxfail", "--no-header",
                                  "--no-summary", "--strict", "--collect-only", "--co", "--setup-show"}
            if not known_flag:
                return None  # An unknown long option may take the next word as its value.
        elif not word.startswith("--") and len(word) == 2 and word not in {"-q", "-v", "-x", "-s", "-l", "-h"}:
            return None
    if any(word in {"--collect-only", "--co", "--junitxml", "--junit-xml"} or word.startswith("--junit")
           for word in options):
        return None
    if any(word.startswith("-") for word in selection):
        return None  # After `--` a test path may look like an option; rebuilt commands would misread it.
    return _PlainPytest(assignments, words[assignments.__len__():index + 1], options, selection)


def _collect(n, ctx, root, plain, options, log):
    """Node ids pytest itself collects for these options; None when collection is not clean."""
    from foragent import runner

    quiet = [word for word in options if word not in _VERBOSITY and not re.fullmatch(r"-[qv]+", word)]
    command = plain.shell([*quiet, "--collect-only", "-q", "-p", "no:cacheprovider"], plain.selection)
    log.unlink(missing_ok=True)
    result = runner._check(command, root, log, max(1, runner._remaining(n, ctx)), progress=lambda: _progress(n, ctx),
                           slot=_test_slot(n, ctx), clock=ctx.monotonic)
    if result.exit_code == 5 and not result.timed_out:
        return []  # pytest collected no tests for these options.
    if result.exit_code != 0 or result.timed_out:
        return None
    text = log.read_text(encoding="utf-8").split("\nCheck exit_code=", 1)[0]
    ids = [line for line in text.splitlines()[1:] if _NODE_LINE.match(line) and line != command]
    if not ids or not all((root / _NODE_LINE.match(node).group(1)).is_file() for node in ids):
        return None  # Output that is not one repository-relative node id per test proves nothing.
    return ids


def _with_local_only(options):
    """These options with a marker expression that also requires local_only."""
    result, words = [], iter(options)
    expression = None
    for word in words:
        if word == "-m":
            expression = next(words)
        else:
            result.append(word)
    return [*result, "-m", f"({expression}) and local_only" if expression else "local_only"]


@contextmanager
def skipping(n, ctx, command, skips, locked=(), label="Done when", *, root):
    """Yield the command to run so that it leaves out what ``skips`` covers (D124).

    pytest itself collects each plain pytest line; the tests CI passed on
    exactly this commit, or that passed earlier in this pass, are subtracted,
    and the rest runs by node id with the line's own invocation and options.
    Locked paths and ``local_only`` tests always stay. A block with any line
    that is not a plain pytest command, or whose collection is not clean, runs
    exactly as written.
    """
    from foragent.run_checks import _append, _passing_acceptance_ids

    lines = command.splitlines()
    plains = [None if not line.strip() else _plain_pytest(line) for line in lines]
    if skips is None or not any(plains) or any(line.strip() and plain is None
                                               for line, plain in zip(lines, plains)):
        if skips is not None:
            _append(ctx.store.log_path(n), f"{label}: no CI skip for this command form; running it as written")
        yield command
        return
    runs = ctx.store.root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    passed = skips.passed()
    with tempfile.TemporaryDirectory(prefix="owner-skip-", dir=runs) as directory:
        rewritten, reports, collected, skipped = [], [], 0, 0
        for i, (line, plain) in enumerate(zip(lines, plains)):
            if plain is None:
                rewritten.append(line)
                continue
            log = Path(directory) / f"collect-{i}.log"
            ids = _collect(n, ctx, root, plain, plain.options, log)
            machine = _collect(n, ctx, root, plain, _with_local_only(plain.options), log) if ids else None
            if not ids or machine is None:
                _append(ctx.store.log_path(n), f"{label}: collection unavailable; running the command as written")
                yield command
                return
            local = set(machine)

            def keep(node):
                path = node.split("::", 1)[0]
                if node in skips.ran:
                    return False
                if node not in passed:
                    return True
                return (any(path == item or path.startswith(item.rstrip("/") + "/") for item in locked)
                        or node in local)

            remaining = [node for node in ids if keep(node)]
            collected += len(ids)
            skipped += len(ids) - len(remaining)
            if not remaining:
                rewritten.append("true")
                continue
            report = Path(directory) / f"line-{i}.xml"
            reports.append(report)
            rewritten.append(plain.shell([*plain.options, f"--junitxml={report}", "-o", "junit_family=legacy"],
                                         remaining))
        _append(ctx.store.log_path(n), f"{label}: skipped {skipped} of {collected} using CI evidence for {skips.head}")
        yield "\n".join(rewritten)
        for report in reports:
            try:
                skips.ran.update(_passing_acceptance_ids(report))
            except (OSError, ET.ParseError):
                continue


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
                  root=attempt.worktree) as selected:
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
                          label="Acceptance comparison", root=attempt.worktree) as selected:
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
