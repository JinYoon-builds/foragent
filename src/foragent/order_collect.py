"""Whether the acceptance files an order publishes collect in the tree main would receive.

This is a factual importability check that never touches the caller's main, index or files.
A scratch copy of the fetched base plus exactly this order's acceptance files is collected,
not executed, with the project's own test command and only the copy and the interpreter's folders
on the import path. A check that cannot establish a result refuses too, and says so apart from an import error.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

from foragent import capacity, order_scratch, processes
from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.i18n import text
from foragent.issue import acceptance_paths

# Execution shares this budget, per order, however many bases it is checked on.
LIMIT_S = 30.0
# Running the tests that import a changed file, on the base and with the order's files, shares this.
IMPORTERS_LIMIT_S = 120.0
_OUTPUT_CHARS = 1500
_EXCERPT_CHARS = 600
# Inherited settings that add import paths or choose another project; the command may set its own.
_CALLER_SETTINGS = "unset PYTHONPATH PYTHONUSERBASE PYTHONPYCACHEPREFIX PYTEST_PLUGINS UV_PROJECT UV_WORKING_DIRECTORY; "
# Placed in the copy and loaded before the acceptance files. Import paths from anywhere (assignments
# in the command, `-o pythonpath`, a path file) leave the search list unless they are in the copy or
# the interpreter's own folders, so nothing else can satisfy an import.
# Which files pytest collects depends on the project's plugins and options, so every file is named.
# A named file no collector takes would stop the whole run; this gives it an empty collector.
_COPY_CONFTEST = """import os, sys

import pytest

kept = [os.path.dirname(os.path.dirname(__file__)), sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix]
kept = [os.path.realpath(path) for path in kept]
sys.path[:] = [entry for entry in sys.path if any(
    os.path.realpath(entry) == root or os.path.realpath(entry).startswith(root + os.sep) for root in kept)]


class Published(pytest.File):
    def collect(self):
        return []


class AnyPublishedFile:
    def pytest_collect_file(self, file_path, parent):
        if file_path.suffix != ".py" and parent.session.isinitpath(file_path):
            return Published.from_parent(parent, path=file_path)


def pytest_configure(config):
    config.pluginmanager.register(AnyPublishedFile(), "foragent-collect-published")
"""


@dataclass(frozen=True)
class Collected:
    """The base commit this order last collected on and the budget used so far."""
    commit: str
    spent: float


def acceptance_files(plan: dict, files: dict[str, str | bytes], config: Config) -> dict[str, str | bytes]:
    """The published contents of this order's acceptance files: explicit and from Done when."""
    names: set[str] = set()
    for item in [*plan["issues"], *plan.get("edits", [])]:
        names.update(item.get("acceptance_files", {}))
        names.update(acceptance_paths(item["done_when"], config.acceptance_dir))
    # A statement issue's tests may not be written yet; they are not in ``files`` then.
    return {name: files[name] for name in sorted(names) if name in files}


def _has_origin(git: GitRepo) -> bool:
    try:
        git._call(["remote", "get-url", "origin"])
    except GitError:
        return False
    return True


def _current_base(git: GitRepo, branch: str) -> str:
    """The commit this order would land on, before the publication lock refreshes the checkout.

    With origin configured a failed refresh is an error: a stale branch certifies nothing.
    Local commits the remote lacks stay, because publication pushes them along.
    """
    if not _has_origin(git):
        return git.ref_sha(f"refs/heads/{branch}")
    git.fetch("origin", branch)
    remote = git.ref_sha(f"refs/remotes/origin/{branch}")
    try:
        local = git.ref_sha(f"refs/heads/{branch}")
    except GitError:
        return remote
    return local if git.is_ancestor(remote, local) else remote


def _write(tree: Path, name: str, content: str | bytes) -> None:
    # The base may hold a link at or above this path; the overlay never writes through it.
    order_scratch._own_parents(tree, name, create=True)
    path = tree / name
    order_scratch._remove_path(path)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def _errors(report: Path, tree: Path) -> list[tuple[str, str]] | None:
    """(path, error text) of each collection error in the report; None without a usable report."""
    try:
        cases = list(ET.parse(report).iter("testcase"))
    except (OSError, ET.ParseError):
        return None
    found = []
    for case in cases:
        if (error := case.find("error")) is not None:
            detail = (error.text or error.get("message") or "").replace(f"{tree}/", "").strip()
            found.append((case.get("file") or case.get("name") or "?", detail[-_OUTPUT_CHARS:]))
    return found


def _output(log: Path, tree: Path) -> str:
    try:
        return log.read_text(encoding="utf-8", errors="replace").replace(f"{tree}/", "").strip()[-_OUTPUT_CHARS:]
    except OSError:
        return ""


class _Unavailable(Exception):
    """The check established no result; the first argument is the cause, the second the command."""


def run_copy(git: GitRepo, base: str, written: dict[str, str | bytes], names: list[str], work: Path,
             config: Config, language: str, deadline: float, *, before: str, after: str, conftest: str,
             timeout: str, slot_timeout: str | None,
             inspect: Callable[[Path], None] | None = None) -> tuple[int, Path, Path, str]:
    """Run the project's pytest on exactly ``names`` in a scratch copy of ``base`` plus ``written``.

    A run that executes tests waits for a machine test slot (``slot_timeout`` is its refusal); a short
    collect-only run passes None and takes no slot, so lane test runs holding every slot never delay it.
    ``inspect`` sees the finished copy before anything runs and may raise _Unavailable.
    Returns (exit code, copy, log, command shown). Raises _Unavailable when no run happened or it did not finish.
    """
    from foragent.order import detect_test_commands
    from foragent.run_checks import _pytest_command

    tree, log = work / "tree", work / "run.log"
    order_scratch.build(git, base, tree)
    for name, content in written.items():
        _write(tree, name, content)
    if inspect is not None:
        inspect(tree)
    # The clean copy decides, so caller-only project files cannot select a command.
    command = config.test_command or next(iter(detect_test_commands(tree)), None)
    if command is None:
        raise _Unavailable(text("order_collect_no_command", language), None)
    pytest = _pytest_command(command)
    if pytest is None:
        raise _Unavailable(text("order_collect_unsupported", language), command)
    attempted = f"{pytest} {before} {shlex.join(names)}"
    # Inside the copy: pytest finds the project's configuration from the common folder of its arguments.
    named = tree / ".foragent-collect"
    named.mkdir()
    (named / "conftest.py").write_text(conftest, encoding="utf-8")
    # Each name is passed as "./name", so pytest reads it as a path: never an option ("-..") or an
    # argument file ("@..").
    paths = shlex.join(f"./{name}" for name in names)
    invocation = f"{_CALLER_SETTINGS}{pytest} {before} {shlex.quote(str(named))} {paths} {after}"
    try:
        # HELD_ENV keeps a `fa test` inside the project's command from queueing for a second slot.
        with (capacity.hold_test_slot(config.test_slots, deadline=deadline, timed_out=lambda: slot_timeout)
              if slot_timeout is not None else contextlib.nullcontext()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(timeout)
            code, timed_out, _ = processes.run_logged(
                ["/bin/sh", "-c", invocation], cwd=tree, timeout_s=remaining, log_path=log,
                env={**capacity.HELD_ENV, "PYTHONDONTWRITEBYTECODE": "1"})
    except (TimeoutError, OSError) as exc:
        raise _Unavailable(str(exc), attempted) from exc
    if timed_out:
        raise _Unavailable(timeout, attempted)
    return code, tree, log, attempted


def _collect(git: GitRepo, base: str, selected: dict[str, str | bytes], work: Path, config: Config,
             language: str, deadline: float) -> list[tuple[str, str]]:
    report = work / "collect.xml"
    code, tree, log, attempted = run_copy(
        git, base, selected, list(selected), work, config, language, deadline, before="--collect-only",
        after=f"-o junit_family=legacy --junit-prefix= --junitxml={shlex.quote(str(report))}",
        conftest=_COPY_CONFTEST, timeout=text("order_collect_timeout", language, seconds=int(LIMIT_S)),
        slot_timeout=None)
    errors = _errors(report, tree)
    if errors:
        return errors
    # 5 is a successful collection without test functions, as for a helper module.
    if errors is None or code not in {0, 5}:
        raise _Unavailable(text("order_collect_exit", language, code=code, output=_output(log, tree)), attempted)
    return []


def _changed(git: GitRepo, base: str, selected: dict[str, str | bytes]) -> list[str]:
    """This order's Python acceptance files whose bytes differ from the base, new ones included."""
    changed = []
    for name, content in selected.items():
        if not name.endswith(".py"):
            continue
        data = content.encode("utf-8") if isinstance(content, str) else content
        current = git.blob_sha(base, name)
        digest = hashlib.sha256 if current is not None and len(current) == 64 else hashlib.sha1
        if current != digest(b"blob %d\0" % len(data) + data).hexdigest():
            changed.append(name)
    return changed


def _module_names(changed: list[str]) -> set[str]:
    """The names a changed file is imported by: its stem, or its package's name for an ``__init__.py``."""
    names = set()
    for name in changed:
        path = PurePosixPath(name)
        if path.stem == "__init__":
            if path.parent.name:
                names.add(path.parent.name)
        elif path.stem != "conftest":
            names.add(path.stem)
    return names


def _importers(git: GitRepo, base: str, changed: list[str], own: set[str]) -> list[str]:
    """Python files on the base that mention a changed module's name as a word; never the order's own or a conftest.

    Every way to import a module by name, however it is spelled or called, contains that name, so this
    is a superset of the direct importers. A file that only mentions the name runs the same on both
    sides, so it costs time and never a refusal.
    """
    names = _module_names(changed)
    if not names:
        return []
    words = [argument for name in sorted(names) for argument in ("-e", name)]
    try:
        # -z prints each path raw: no quoting of non-ASCII or special characters.
        output = git._call(["grep", "-z", "-l", "-w", "-F", *words, base, "--", "*.py"])
    except GitError as exc:
        cause = exc.__cause__
        if isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 1:
            return []  # No file mentions a changed module.
        raise
    found = set()
    for entry in output.split("\0"):
        name = entry[len(base) + 1:] if entry.startswith(f"{base}:") else entry
        if name and name not in own and PurePosixPath(name).name != "conftest.py":
            found.add(name)
    return sorted(found)


# Added to the copy's conftest for importer runs: one JSON row per fact, read instead of pytest's text.
# Registered as a plugin, so it sees reports for files outside its folder. Names are paths in the copy.
_RESULTS = """

import json as _json


class ImporterResults:
    def __init__(self, config):
        self.config = config
        self.tree = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.out = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.jsonl"), "a",
                        encoding="utf-8")
        self.names = {}

    def write(self, row):
        self.out.write(_json.dumps(row) + "\\n")
        self.out.flush()

    def path(self, path):
        return os.path.relpath(os.path.realpath(str(path)), os.path.realpath(self.tree)).replace(os.sep, "/")

    def key(self, item):
        # Identity without display ids: the function and each parameter's position, so a changed
        # parameter label is the same test, not a new one.
        rest = item.nodeid[item.nodeid.find("::"):] if "::" in item.nodeid else "::" + item.name
        function = rest.split("[", 1)[0]
        callspec = getattr(item, "callspec", None)
        indices = getattr(callspec, "indices", None) or {}
        suffix = "[" + ",".join(f"{name}={indices[name]}" for name in sorted(indices)) + "]" if indices else ""
        return self.path(item.path) + function + suffix, self.path(item.path) + rest

    def pytest_collection_finish(self, session):
        tests = []
        for item in session.items:
            key, shown = self.key(item)
            self.names[item.nodeid] = key
            tests.append({"key": key, "name": shown})
        self.write({"phase": "items", "tests": tests})

    def pytest_collectreport(self, report):
        if report.failed:
            path = os.path.join(str(self.config.rootpath), report.nodeid.split("::")[0])
            self.write({"phase": "collect", "file": self.path(path), "error": str(report.longrepr)})

    def pytest_runtest_logreport(self, report):
        self.write({"phase": report.when, "test": self.names.get(report.nodeid, report.nodeid),
                    "outcome": report.outcome, "error": str(report.longrepr) if report.failed else ""})

    def pytest_keyboard_interrupt(self, excinfo):
        # Also called for pytest.exit(), whatever return code it asks for.
        self.write({"phase": "interrupted", "error": str(excinfo.value)})

    def pytest_sessionfinish(self, session, exitstatus):
        self.write({"phase": "finish", "status": int(exitstatus)})


_published_configure = pytest_configure


def pytest_configure(config):
    _published_configure(config)
    config.pluginmanager.register(ImporterResults(config), "foragent-importer-results")
"""


@dataclass
class _Run:
    """What one importer run established, by test identity.

    ``complete`` means the session finished uninterrupted and every collected test reported its teardown.
    """
    complete: bool
    names: dict[str, str]
    passed: set[str]
    failed: dict[str, str]
    uncollected: dict[str, str]


def _results(path: Path, tree: Path) -> _Run:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        rows = []
    names: dict[str, str] | None = None
    finished, interrupted, torn_down = False, False, set()
    outcomes: dict[str, set[str]] = {}
    failed: dict[str, str] = {}
    uncollected: dict[str, str] = {}
    for row in rows:
        phase = row.get("phase")
        if phase == "items":
            names = {test["key"]: test["name"] for test in row["tests"]}
        elif phase == "finish":
            finished = True
        elif phase == "interrupted":
            interrupted = True
        elif phase == "collect":
            uncollected.setdefault(row["file"], row["error"].replace(f"{tree}/", ""))
        elif phase in {"setup", "call", "teardown"}:
            outcomes.setdefault(row["test"], set()).add(row["outcome"])
            if row["outcome"] == "failed":
                failed.setdefault(row["test"], row["error"].replace(f"{tree}/", ""))
            if phase == "teardown":
                torn_down.add(row["test"])
    complete = (names is not None and finished and not interrupted
                and all(test in torn_down for test in names))
    passed = {test for test, seen in outcomes.items() if seen == {"passed"} and test in torn_down}
    return _Run(complete, names or {}, passed, failed, uncollected)


def _excerpt(why: str) -> str:
    """The lines that say why: pytest's error lines when there are any, else the end of the report."""
    lines = [line for line in why.splitlines() if line.startswith("E ")]
    return "\n".join(lines[-6:] or why.splitlines()[-6:])[-_EXCERPT_CHARS:]


def _in_file(test: str, file: str) -> bool:
    return test.startswith(file + "::")


def _run_importers(git: GitRepo, base: str, selected: dict[str, str | bytes], importers: list[str], work: Path,
                   config: Config, language: str, deadline: float) -> list[tuple[str, str]]:
    """(test, why) of each importer test that passes on the base and does not pass with the order's files."""
    runs = []
    for label, written in (("base", {}), ("order", selected)):
        folder = work / label
        folder.mkdir()
        code, tree, log, attempted = run_copy(
            git, base, written, importers, folder, config, language, deadline,
            before="-p no:cacheprovider --continue-on-collection-errors", after="",
            conftest=_COPY_CONFTEST + _RESULTS,
            timeout=text("order_collect_importers_timeout", language, seconds=int(IMPORTERS_LIMIT_S)),
            slot_timeout=None)
        run = _results(tree / ".foragent-collect" / "results.jsonl", tree)
        # Without a complete base run there is nothing to compare with.
        if label == "base" and not run.complete:
            raise _Unavailable(text("order_collect_importers_incomplete", language, code=code,
                                    output=_output(log, tree)), attempted)
        runs.append((run, code, _output(log, tree)))
    (before, _, _), (after, code, output) = runs
    stopped = text("order_collect_importers_stopped", language, code=code, output=_excerpt(output))
    gone = text("order_collect_importers_gone", language)
    # A function that passed on the base in any of its parameters: its new failing parameters count too.
    working = {_function(test) for test in before.passed}
    broken: list[tuple[str, str]] = []
    for file, why in sorted(after.uncollected.items()):
        if any(_in_file(test, file) for test in before.passed):
            broken.append((file, _excerpt(why)))
    for test in sorted(set(before.passed) | set(after.failed)):
        if test in after.passed or any(_in_file(test, file) for file in after.uncollected):
            continue
        if test not in before.passed and (test in before.names or _function(test) not in working):
            continue  # Red on the base, or new in a function that never passed there.
        shown = after.names.get(test) or before.names.get(test) or test
        if test in after.failed:
            broken.append((shown, _excerpt(after.failed[test])))
        elif not after.complete:
            # The order's files stopped the run before this test passed: that is not a pass.
            broken.append((shown, stopped))
        elif test not in after.names:
            broken.append((shown, gone))
    return broken


def _function(test: str) -> str:
    return test.split("[", 1)[0]


def check(plan: dict, files: dict[str, str | bytes], root: Path, config: Config, git: GitRepo | None = None, *,
          base: str | None = None, earlier: Collected | None = None) -> Collected | None:
    """Refuse unless this order's acceptance files collect on the base; None when it has none.

    ``base`` is a commit another order was just checked on, so one batch shares one base.
    ``earlier`` is this order's previous result: the same base is not collected twice and a
    new base gets only what remains of the budget.
    """
    from foragent.order import PlanError

    selected = acceptance_files(plan, files, config)
    if not selected:
        return None
    language = (getattr(config, "language", None) or "en").lower().split("-")[0]

    def unavailable(cause: str, command: str | None) -> PlanError:
        return PlanError(text("order_collect_unavailable", language, files=", ".join(selected),
                              command=command or text("order_collect_no_command_name", language), cause=cause))

    git = git or GitRepo(root)
    try:
        base = base or _current_base(git, config.base_branch)
    except (GitError, OSError, subprocess.SubprocessError) as exc:
        cause = text("order_collect_no_base", language, branch=config.base_branch, error=" ".join(str(exc).split()))
        raise unavailable(cause, config.test_command) from exc
    if earlier is not None and earlier.commit == base:
        return earlier
    spent = earlier.spent if earlier is not None else 0.0
    started = time.monotonic()
    work = None
    try:
        work = Path(tempfile.mkdtemp(prefix="foragent-collect-"))
        errors = _collect(git, base, selected, work, config, language, started + LIMIT_S - spent)
    except _Unavailable as exc:
        raise unavailable(exc.args[0], exc.args[1]) from exc
    except Exception as exc:  # Whatever stopped the check, it did not establish a pass.
        raise unavailable(f"{type(exc).__name__}: {' '.join(str(exc).split())}", config.test_command) from exc
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    if errors:
        raise PlanError(text("order_collect_refused", language, base=base[:12], files=", ".join(selected),
                             errors="\n".join(f"{path}:\n{detail}" for path, detail in errors)))
    collected = Collected(base, spent + time.monotonic() - started)
    _check_importers(git, base, selected, config, language)
    return collected


def _check_importers(git: GitRepo, base: str, selected: dict[str, str | bytes], config: Config,
                     language: str) -> None:
    """Refuse when a changed file breaks a test that imports it and passes on the base."""
    from foragent.order import PlanError

    work = None
    changed: list[str] = []
    importers: list[str] = []
    try:
        changed = _changed(git, base, selected)
        importers = _importers(git, base, changed, set(selected)) if changed else []
        if not importers:
            return
        work = Path(tempfile.mkdtemp(prefix="foragent-importers-"))
        broken = _run_importers(git, base, selected, importers, work, config, language,
                                time.monotonic() + IMPORTERS_LIMIT_S)
    except _Unavailable as exc:
        raise PlanError(text("order_collect_importers_unavailable", language, files=", ".join(changed),
                             tests=", ".join(importers), cause=exc.args[0],
                             command=exc.args[1] or text("order_collect_no_command_name", language))) from exc
    except Exception as exc:  # Whatever stopped the check, it did not establish a pass.
        raise PlanError(text("order_collect_importers_unavailable", language, files=", ".join(changed),
                             tests=", ".join(importers), cause=f"{type(exc).__name__}: {' '.join(str(exc).split())}",
                             command=config.test_command or text("order_collect_no_command_name", language))) from exc
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    if broken:
        raise PlanError(text("order_collect_importers_refused", language, base=base[:12], files=", ".join(changed),
                             tests="\n".join(f"{test}:\n{why}" for test, why in broken)))


def land(git: GitRepo, branch: str, commit: str | None) -> None:
    """Move the checkout onto exactly the commit just collected on, never a newer one.

    A later remote move stays unchecked here and the push refuses it as a non-fast-forward.
    """
    if commit is None:
        git.update_base(branch)
    elif not git.is_ancestor(commit, "HEAD"):
        git._call(["merge", "--ff-only", "--no-edit", commit])
