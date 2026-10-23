"""Acceptance test for baseline-cache. Locked by Foragent; do not edit."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import shlex
import sys
from threading import Event, Lock
import xml.etree.ElementTree as ET

import pytest

from conftest import ACCEPTANCE, GitHistory, git_command
from foragent.runner import run_issue
from foragent.state import StateStore
from test_acceptance_baseline import FUTURE, KEEP
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


TEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"
OTHER_DIR = "checks/acceptance/"
KEEP_SOURCE = "from pathlib import Path\n\ndef test_keep():\n    assert Path('keep.txt').is_file()\n"
FUTURE_SOURCE = "def test_future():\n    assert False, 'implemented by another issue'\n"


def prepare_history(history):
    history.update({
        "keep.txt": "keep\n",
        KEEP: KEEP_SOURCE,
        FUTURE: FUTURE_SOURCE,
        f"{OTHER_DIR}test_keep.py": KEEP_SOURCE,
        f"{OTHER_DIR}test_future.py": FUTURE_SOURCE,
    })
    git_command(history.clone, "fetch", "-q", "origin")
    git_command(history.clone, "merge", "-q", "--ff-only", "origin/main")


def another_repository(history, directory):
    """Independent Git metadata/origin, with the exact same base commit."""
    directory.mkdir()
    origin, clone = directory / "origin.git", directory / "clone"
    git_command(directory, "clone", "--bare", str(history.origin), str(origin))
    git_command(directory, "clone", str(origin), str(clone))
    assert git_command(clone, "rev-parse", "HEAD") == history.tip
    return GitHistory(origin, clone, history.tip)


def cache_case(history, directory, monkeypatch, *, number, repo="owner/repo", **settings):
    directory.mkdir(parents=True, exist_ok=True)
    case = make_case(history, directory, monkeypatch, **{
        "skill_libraries": [], "test_command": TEST_COMMAND, "repo": repo, **settings,
    })
    snapshot = asdict(case.store.load_approval(1))
    snapshot["number"] = number
    if number != 1:
        case.tracker.add_issue(number, "Implement feature", snapshot["body"],
                               labels=["approved", "running"])
    # Fresh store/context objects must share the machine's data root, rather
    # than putting a baseline next to a particular issue's run record.
    case.store = case.ctx.store = StateStore.for_repo(repo, now=lambda: case.clock[0])
    case.ctx.repo = repo
    case.store.save_approval(**snapshot)
    case.store.start_run(number, branch=f"foragent/issue-{number}",
                         worktree=case.store.worktree_path(number))
    case.number = number
    case.base_sha = history.tip
    directory_name = case.config.acceptance_dir.rstrip("/")
    case.passing_ids = {f"{directory_name}/test_keep.py::test_keep"}
    if directory_name == "tests/acceptance":
        case.passing_ids.add(f"{ACCEPTANCE}::test_feature")
    case.future_id = f"{directory_name}/test_future.py::test_future"
    return case


class BaselineProcesses:
    """Observe real runner checks; fake only baseline discovery subprocesses.

    Verification and Done-when commands execute for real, including an actual
    pytest failure when an implementer removes keep.txt. Exit 1 with ordinary
    test failures is a usable D53 baseline, unlike timeout/collection failures.
    """

    def __init__(self, monkeypatch, *, first_result="usable", on_build=None):
        from foragent import runner

        self.cases = {}
        self.commands = []
        self.builds = []
        self.lock = Lock()
        original = runner.run_logged

        def process(argv, *, cwd, **kwargs):
            assert argv[:2] == ["/bin/sh", "-c"], argv
            case = self.cases[Path(kwargs["log_path"])]
            words = shlex.split(argv[2])
            report = None
            for index, word in enumerate(words):
                if word.startswith(("--junitxml=", "--junit-xml=")):
                    report = Path(word.split("=", 1)[1])
                elif word in {"--junitxml", "--junit-xml"}:
                    report = Path(words[index + 1])
            with self.lock:
                self.commands.append((case, words, Path(cwd)))
                if report is not None:
                    self.builds.append((case, words, Path(cwd)))
                    ordinal = len(self.builds)
            if report is None:
                return original(argv, cwd=cwd, **kwargs)

            assert Path(cwd) != case.store.worktree_path(case.number)
            assert git_command(Path(cwd), "rev-parse", "HEAD") == case.base_sha
            assert case.config.acceptance_dir in words
            if on_build is not None:
                on_build(case)
            result = first_result if ordinal == 1 else "usable"
            if result == "no-junit":
                return 0, False, None

            suite = ET.Element("testsuite")
            for node_id in sorted(case.passing_ids | {case.future_id}):
                path, name = node_id.split("::")
                test = ET.SubElement(suite, "testcase", file=path, name=name,
                                     classname=path.removesuffix(".py").replace("/", "."))
                if node_id == case.future_id:
                    ET.SubElement(test, "failure", message="another issue is not implemented")
            if result == "collection-error":
                error = ET.SubElement(suite, "testcase", name="test_broken", classname="pytest")
                ET.SubElement(error, "error", message="collection failed: SyntaxError")
            report.parent.mkdir(parents=True, exist_ok=True)
            ET.ElementTree(suite).write(report, encoding="unicode")
            if result == "timeout":
                # A timeout can leave a parseable but incomplete report behind.
                return None, True, None
            return (2 if result == "collection-error" else 1), False, None

        monkeypatch.setattr(runner, "run_logged", process)

    def register(self, case):
        self.cases[case.store.log_path(case.number)] = case
        return case

    def assert_protected(self, case):
        prompt = case.implementer.calls[0]["prompt"]
        assert "already pass" in prompt
        assert all(node_id in prompt for node_id in case.passing_ids)
        assert "test_future" not in prompt
        verification = [words for owner, words, cwd in self.commands
                        if owner is case and cwd == case.store.worktree_path(case.number)
                        and "pytest" in words]
        assert verification, "The passing baseline must still be checked on the implementation"
        for words in verification:
            selectors = {word for word in words if "::" in word}
            assert selectors == case.passing_ids, words
            assert case.config.acceptance_dir not in words


def assert_merged(case, outcome):
    assert outcome == "merged", (outcome, case.store.log_path(case.number).read_text())
    assert len(case.tracker.merged_prs) == 1
    assert not case.tracker.get_issue(case.number).labels & {"blocked", "needs-human"}


def assert_cache_written(home):
    cache = home / ".local/share/foragent/baselines"
    assert cache.is_dir(), f"No machine-wide baseline cache at {cache}"
    assert any(path.is_file() for path in cache.rglob("*")), "Baseline cache was not persisted"


@pytest.mark.parametrize("scope", ["another-issue", "another-repository-store"])
def test_baseline_cache_reuses_passing_ids_and_still_retries_regressions(
    git_history, tmp_path, monkeypatch, isolated_home, scope,
):
    prepare_history(git_history)
    checks = BaselineProcesses(monkeypatch)
    first = checks.register(cache_case(git_history, tmp_path / "first", monkeypatch, number=1))
    assert_merged(first, run_issue(first.number, first.ctx))
    assert len(checks.builds) == 1
    checks.assert_protected(first)

    history, repo = git_history, first.ctx.repo
    if scope == "another-repository-store":
        history = another_repository(git_history, tmp_path / "other-repository")
        repo = "owner/other"
    second = checks.register(cache_case(history, tmp_path / "second", monkeypatch,
                                        number=2, repo=repo))
    assert (first.store.root == second.store.root) == (scope == "another-issue")

    def break_keep(kwargs):
        second.implement(kwargs)
        (kwargs["worktree"] / "keep.txt").unlink()

    def restore_keep(kwargs):
        (kwargs["worktree"] / "keep.txt").write_text("keep\n")

    second.implementer.steps = [break_keep, restore_keep]
    assert_merged(second, run_issue(second.number, second.ctx))
    assert len(second.implementer.calls) == 2
    retry = second.implementer.calls[1]["prompt"]
    assert "test_keep" in retry and "AssertionError" in retry
    assert "test_future" not in retry
    checks.assert_protected(second)
    assert len(checks.builds) == 1, (
        f"Same-key {scope} ran baseline pytest {len(checks.builds)} times; expected one"
    )
    assert_cache_written(isolated_home)


@pytest.mark.parametrize("changed_key", ["base-sha", "test-command", "acceptance-dir"])
def test_baseline_cache_builds_and_then_reuses_each_distinct_key(
    git_history, tmp_path, monkeypatch, isolated_home, changed_key,
):
    prepare_history(git_history)
    checks = BaselineProcesses(monkeypatch)
    first = checks.register(cache_case(git_history, tmp_path / "first", monkeypatch, number=1))
    assert_merged(first, run_issue(first.number, first.ctx))
    assert len(checks.builds) == 1
    checks.assert_protected(first)

    settings = {}
    if changed_key == "base-sha":
        git_history.update({"base-revision.txt": "new base, same acceptance tests\n"})
        assert git_history.tip != first.base_sha
    elif changed_key == "test-command":
        settings["test_command"] = TEST_COMMAND + " -v"
    else:
        settings["acceptance_dir"] = OTHER_DIR

    second = checks.register(cache_case(git_history, tmp_path / "second", monkeypatch,
                                        number=2, **settings))
    assert_merged(second, run_issue(second.number, second.ctx))
    assert len(checks.builds) == 2, f"Changing {changed_key} reused an incompatible baseline"
    checks.assert_protected(second)

    third = checks.register(cache_case(git_history, tmp_path / "third", monkeypatch,
                                       number=3, **settings))
    assert_merged(third, run_issue(third.number, third.ctx))
    checks.assert_protected(third)
    assert len(checks.builds) == 2, (
        f"New {changed_key} baseline was rebuilt on its cache hit: "
        f"{len(checks.builds)} baseline pytest runs; expected two"
    )
    assert_cache_written(isolated_home)


def test_baseline_cache_concurrent_same_key_waits_for_one_build(
    git_history, tmp_path, monkeypatch, isolated_home,
):
    prepare_history(git_history)
    history = another_repository(git_history, tmp_path / "other-repository")
    first = cache_case(git_history, tmp_path / "first", monkeypatch, number=1)
    second = cache_case(history, tmp_path / "second", monkeypatch, number=2, repo="owner/other")
    entered, release, duplicate, second_ready = Event(), Event(), Event(), Event()

    def hold_first_build(case):
        if case is first:
            entered.set()
            assert release.wait(15), "Test did not release the first baseline subprocess"
        else:
            duplicate.set()

    checks = BaselineProcesses(monkeypatch, on_build=hold_first_build)
    checks.register(first)
    checks.register(second)
    factory = second.ctx.executor_factory

    def observe_factory(name):
        # Executor construction precedes discovery; its run method must wait
        # for the shared baseline to finish before implementation can start.
        executor = factory(name)
        if name == second.config.executor:
            second_ready.set()
        return executor

    second.ctx.executor_factory = observe_factory
    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(run_issue, first.number, first.ctx)
        try:
            assert entered.wait(10), "First run never started baseline discovery"
            waiter = pool.submit(run_issue, second.number, second.ctx)
            assert second_ready.wait(10), "Second runner never reached baseline preparation"
            overlapped = duplicate.wait(0.5)
            implemented_early = bool(second.implementer.calls)
            finished_early = waiter.done()
        finally:
            release.set()
        assert_merged(first, holder.result(timeout=20))
        assert_merged(second, waiter.result(timeout=20))

    checks.assert_protected(first)
    checks.assert_protected(second)
    assert len(checks.builds) == 1, (
        f"Concurrent same-key runs executed baseline pytest {len(checks.builds)} times; expected one"
    )
    assert not overlapped, "Second baseline subprocess overlapped the first build"
    assert not implemented_early and not finished_early, "Second runner did not wait for the baseline"
    assert_cache_written(isolated_home)


@pytest.mark.parametrize("unusable", ["timeout", "no-junit", "collection-error"])
def test_baseline_cache_rebuilds_unusable_result_then_caches_recovery(
    git_history, tmp_path, monkeypatch, isolated_home, unusable,
):
    prepare_history(git_history)
    checks = BaselineProcesses(monkeypatch, first_result=unusable)
    first = checks.register(cache_case(git_history, tmp_path / "first", monkeypatch, number=1))
    # This contract concerns reuse, not whether a run with an unavailable
    # baseline proceeds or stops. Either policy must allow a later run to retry.
    run_issue(first.number, first.ctx)
    assert len(checks.builds) == 1

    second = checks.register(cache_case(git_history, tmp_path / "second", monkeypatch, number=2))
    assert_merged(second, run_issue(second.number, second.ctx))
    assert len(checks.builds) == 2, f"The unusable {unusable} baseline was cached"
    checks.assert_protected(second)

    third = checks.register(cache_case(git_history, tmp_path / "third", monkeypatch, number=3))
    assert_merged(third, run_issue(third.number, third.ctx))
    checks.assert_protected(third)
    assert len(checks.builds) == 2, (
        f"Recovered baseline after {unusable} was not reused: "
        f"{len(checks.builds)} baseline pytest runs; expected two"
    )
    assert_cache_written(isolated_home)
