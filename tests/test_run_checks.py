from dataclasses import replace
from contextlib import contextmanager
from datetime import timedelta
import os
import shlex
import sys
import xml.etree.ElementTree as ET

import pytest

from foragent.config import Config
from foragent.git import GitRepo
from foragent.reasons import Reason
from foragent.runner import CheckResult, run_issue

from conftest import ACCEPTANCE, git_command
from runner_support import COMMAND, batch_setup, run_case, seed_batch, set_done_when


def test_generated_source_is_rechecked_and_committed_instead_of_old_index(run_case):
    c = run_case
    c.executor.steps = [{
        "feature.py": "raise SystemExit(1)\n",
        "generate.sh": "printf 'print(42)\\n' > feature.py\n",
    }]
    set_done_when(c, ["sh generate.sh", "python3 feature.py"])
    assert run_issue(1, c.ctx) == "merged"
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.py") == "print(42)"
    assert c.store.log_path(1).read_text().count("Done when command:") == 4
    assert len(c.executor.calls) == 1


def test_second_verification_failure_reaches_executor_retry(run_case):
    c = run_case
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    # The first run changes a passing input into a failing one. Only the second
    # verification sees the failure; the executor must then repair it.
    set_done_when(c, [
        "if test ! -f .foragent/generated; then mkdir -p .foragent; "
        "touch .foragent/generated; echo broken > feature.txt; else "
        'test "$(cat feature.txt)" = done; fi',
    ])
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 2
    assert "A previous attempt failed" in c.executor.calls[1]["prompt"]
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "done"


@pytest.mark.parametrize("command", [
    "printf x >> feature.txt",
    "if test -f feature.txt; then mv feature.txt other.txt; else mv other.txt feature.txt; fi",
])
def test_checks_that_keep_changing_blobs_or_paths_fail_attempt(run_case, command):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0)
    set_done_when(c, [command])
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert c.tracker.created_prs == c.tracker.merged_prs == []
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "Done when commands modify tracked files" in c.tracker.list_comments(1)[-1].body
    assert c.store.log_path(1).read_text().count("Done when command:") == 2


def test_no_changes_are_a_failed_attempt_with_retry_output(run_case):
    c = run_case
    c.executor.steps = [{}, {}]
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert "The session made no changes." in c.executor.calls[1]["prompt"]
    assert "The session made no changes." in c.tracker.list_comments(1)[-1].body
    assert c.tracker.created_prs == []
    assert git_command(c.history.origin, "rev-parse", "foragent/issue-1") == c.history.tip


@pytest.mark.parametrize("source", ["src/demo/service.py", "demo/service.py"])
def test_related_units_include_importers_of_deleted_modules_and_existing_changed_tests(tmp_path, source):
    from foragent.runner import _related_unit_tests

    files = {
        "tests/test_direct.py": "import demo.service as service\n",
        "tests/unit/service_test.py": "from demo import service as target\n",
        "tests/test_symbols.py": "def test_local_import():\n    from demo.service import run\n",
        "tests/test_unrelated.py": '# import demo.service\nEXAMPLE = "from demo import service"\nimport demo.other\n',
        "tests/test_changed with spaces.py": "broken syntax is still selected!",
        "tests/test_old_broken.py": "broken syntax!",
        "tests/locked/test_acceptance.py": "import demo.service\n",
        "tests/helpers.py": "import demo.service\n",
    }
    for name, contents in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    assert _related_unit_tests(tmp_path, [source, "tests/test_changed with spaces.py", "tests/test_deleted.py"],
                               "./tests/locked/") == (
        "tests/test_changed with spaces.py", "tests/test_direct.py", "tests/test_symbols.py",
        "tests/unit/service_test.py",
    )
    assert _related_unit_tests(tmp_path, ["src/demo/__init__.py"], "tests/locked") == (
        "tests/test_direct.py", "tests/test_symbols.py", "tests/test_unrelated.py", "tests/unit/service_test.py",
    )


@pytest.mark.parametrize("own_only", [False, True])
def test_related_units_exclude_other_modules_tests_after_main_moves(git_history, own_only):
    from foragent.runner import _related_unit_tests

    files = {
        "src/demo/state.py": "def load():\n    return 'old'\n",
        "src/demo/runner.py": "def run():\n    return 'old'\n",
        "tests/test_state.py": "from demo.state import load\n",
        "tests/test_runner.py": "from demo.runner import run\n",
    }
    git_history.update(files)
    git = GitRepo(git_history.clone)
    git.fetch()
    git.merge_base("origin/main", commit=False)
    (git.root / "src/demo/state.py").write_text("def load():\n    return 'changed'\n")
    git.stage_changes()
    git_history.update({
        "src/demo/runner.py": "def run():\n    return 'other run'\n",
        "tests/test_runner.py": "from demo.runner import run\n# another run's test\n",
        "tests/test_other.py": "from demo.runner import run\n",
    })
    git.fetch()
    assert _related_unit_tests(git.root, git.changed_paths("origin/main"), "tests/acceptance",
                               own_only=own_only) == ("tests/test_state.py",)


def test_related_units_own_only_keeps_changed_tests_and_module_counterparts(tmp_path):
    from foragent.runner import _related_unit_tests

    files = {
        "tests/test_service.py": "import demo.service\n",
        "tests/unit/service_test.py": "from demo import service\n",
        "tests/test_direct.py": "import demo.service as service\n",
        "tests/test_changed.py": "def test_changed():\n    pass\n",
        "tests/test_demo.py": "import demo\n",
        "tests/locked/test_service.py": "import demo.service\n",
    }
    for name, contents in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    assert _related_unit_tests(tmp_path, ["src/demo/service.py", "tests/test_changed.py"], "tests/locked",
                               own_only=True) == (
        "tests/test_changed.py", "tests/test_service.py", "tests/unit/service_test.py",
    )
    assert _related_unit_tests(tmp_path, ["src/demo/__init__.py"], "tests/locked", own_only=True) == (
        "tests/test_demo.py",
    )
    assert _related_unit_tests(tmp_path, ["README.md"], "tests/locked", own_only=True) == ()


@pytest.mark.parametrize("module", ["run_checks", "run_review"])
def test_related_units_own_only_selects_runner_module_test_file(module):
    from pathlib import Path
    from foragent.run_checks import _related_unit_tests

    worktree = Path(__file__).resolve().parents[1]
    assert _related_unit_tests(
        worktree, [f"src/foragent/{module}.py"], "tests/acceptance/", own_only=True,
    ) == (f"tests/test_{module}.py",)


@pytest.mark.parametrize(("name", "workflow", "expected"), [
    ("ci.yml", "on: pull_request\njobs: {}\n", True),
    ("ci.yaml", "on: [push, pull_request]\njobs: {}\n", True),
    ("ci.yml", "on:\n  pull_request:\n    branches: [main]\njobs: {}\n", True),
    ("ci.yml", "'on':\n  pull_request: null\njobs: {}\n", True),
    ("ci.yml", "on:\n  push:\n    branches: [main]\njobs: {}\n", False),
    ("ci.yml", "on: [pull_request_target]\njobs: {}\n", False),
    ("ci.yml", "on: [pull_request\n", False),
    ("notes.txt", "on: pull_request\n", False),
])
def test_pull_request_ci_is_read_from_workflow_files(tmp_path, name, workflow, expected):
    from foragent.runner import has_pull_request_ci, trusts_ci

    assert has_pull_request_ci(tmp_path) is False
    path = tmp_path / ".github" / "workflows" / name
    path.parent.mkdir(parents=True)
    path.write_text(workflow)
    assert has_pull_request_ci(tmp_path) is expected
    assert trusts_ci(tmp_path, Config()) is expected
    assert trusts_ci(tmp_path, Config(runner_unit_tests="ci")) is True
    assert trusts_ci(tmp_path, Config(runner_unit_tests="all")) is False


@pytest.mark.parametrize("repaired", [True, False])
def test_related_unit_failure_uses_implementation_budget_and_reaches_next_prompt(run_case, repaired):
    c = run_case
    c.ctx.config = replace(c.ctx.config, test_command=f"{shlex.quote(sys.executable)} -m pytest -q -k ignored")
    c.executor.steps = [
        {"feature.txt": "done", "unit.txt": "bad", "tests/test_new.py": (
            "from pathlib import Path\n"
            "def test_related():\n"
            '    assert Path("unit.txt").read_text() == "good", "related unit failed"\n'
        )},
        {"unit.txt": "good"} if repaired else {},
    ]
    assert run_issue(1, c.ctx) == ("merged" if repaired else "retries-exceeded")
    assert len(c.executor.calls) == 2
    assert "related unit failed" in c.executor.calls[1]["prompt"]
    assert "tests/test_new.py" in c.executor.calls[1]["prompt"]
    assert c.store.get_run(1).attempts == 2
    assert bool(c.tracker.merged_prs) == repaired


@pytest.mark.parametrize("timed_out", [False, True])
def test_related_unit_check_uses_current_budget_excluding_waits(run_case, monkeypatch, timed_out):
    c = run_case
    c.ctx.config = replace(c.ctx.config, test_command="pytest -q")
    c.executor.steps = [{"feature.txt": "done", "tests/test_new.py": "def test_new():\n    assert True\n"}]
    c.now[0] += timedelta(minutes=60)
    c.store.update_run(1, waited_seconds=600)
    calls = []

    def check(command, worktree, log, timeout_s, **kwargs):
        report = next((word.split("=", 1)[1] for word in shlex.split(command) if word.startswith("--junitxml=")), None)
        if report:
            ET.ElementTree(ET.Element("testsuite")).write(report, encoding="unicode")
        selected = "tests/test_new.py" in shlex.split(command)
        if worktree == c.store.worktree_path(1):
            calls.append((command, timeout_s))
            c.now[0] += timedelta(seconds=120)
        killed = selected and timed_out
        return CheckResult(command, -9 if killed else 0, killed, "")

    monkeypatch.setattr("foragent.runner._check", check)
    assert run_issue(1, c.ctx) == ("timeout" if timed_out else "merged")
    assert [limit for _, limit in calls] == [900, 900, 2160]
    assert "tests/test_new.py" in shlex.split(calls[-1][0])
    assert len(c.executor.calls) == c.store.get_run(1).attempts == 1
    if timed_out:
        assert c.store.load_flag(1).reason == Reason.TIMEOUT
        assert c.store.get_run(1).detail == "Related unit tests timed out after 2160 s (1 selected tests)."
        assert c.tracker.created_prs == []


@pytest.mark.parametrize("max_retries", [0, 1])
def test_related_check_timeout_preserves_implementation_before_cleanup(run_case, monkeypatch, max_retries):
    c = run_case
    c.history.update({
        "src/sample.py": "VALUE = 1\n",
        "tests/test_sample.py": "from sample import VALUE\n",
    })
    c.ctx.config = replace(c.ctx.config, test_command="pytest -q", max_retries=max_retries)
    c.executor.steps = [{"feature.txt": "done", "src/sample.py": "VALUE = 2\n"}]
    c.now[0] += timedelta(minutes=70)

    def check(command, worktree, log, timeout_s, **kwargs):
        report = next((word.split("=", 1)[1] for word in shlex.split(command) if word.startswith("--junitxml=")), None)
        if report:
            ET.ElementTree(ET.Element("testsuite")).write(report, encoding="unicode")
        timed_out = "tests/test_sample.py" in shlex.split(command)
        if timed_out:
            assert timeout_s == 1200
            c.now[0] += timedelta(seconds=timeout_s)
        return CheckResult(command, -9 if timed_out else 0, timed_out, "")

    monkeypatch.setattr("foragent.runner._check", check)
    assert run_issue(1, c.ctx) == "timeout"
    assert not c.store.worktree_path(1).exists()
    assert git_command(c.history.clone, "show", "foragent/issue-1:src/sample.py") == "VALUE = 2"
    assert git_command(c.history.origin, "show", "foragent/issue-1:src/sample.py") == "VALUE = 2"
    assert len(c.executor.calls) == c.store.get_run(1).attempts == 1
    assert c.store.load_flag(1).reason == Reason.TIMEOUT
    assert c.store.get_run(1).detail == "Related unit tests timed out after 1200 s (1 selected tests)."
    assert c.tracker.created_prs == c.tracker.merged_prs == []


@pytest.mark.parametrize("slow_check", ["baseline", "done-when", "related"])
def test_silent_checks_report_progress_and_survive_scheduler_stall_limit(run_case, monkeypatch, slow_check):
    from foragent.scheduler import TickContext, TickReport, _recover_runs

    c = run_case
    baseline_file = "tests/acceptance/test_keep.py"
    if slow_check == "baseline":
        c.history.update({baseline_file: "def test_keep():\n    assert True\n"})
    stall_minutes = 30 if slow_check == "related" else 1
    c.ctx.config = replace(c.ctx.config, test_command="pytest -q", stall_minutes=stall_minutes)
    if slow_check == "related":
        c.now[0] += timedelta(minutes=30)
    c.executor.steps = [{"feature.txt": "done", "tests/test_new.py": "def test_new():\n    assert True\n"}]
    killed = []
    scheduler = TickContext(
        repo=c.ctx.repo, repo_root=c.ctx.repo_root, tracker=c.tracker, store=c.store,
        config=c.ctx.config, git=c.ctx.git, now=c.ctx.now, kill_runner=killed.append,
        launcher=lambda number: pytest.fail("check must keep the current runner"), start_runs=False,
    )
    polls = []
    observed = []

    def watch(execute, poll):
        polls.append(poll)
        try:
            return execute()
        finally:
            polls.pop()

    def check_process(argv, *, cwd, timeout_s, log_path, **kwargs):
        command = argv[2]
        words = shlex.split(command)
        report = next((word.split("=", 1)[1] for word in words if word.startswith("--junitxml=")), None)
        if report:
            kind = "baseline"
        elif command == COMMAND:
            kind = "done-when"
        elif "tests/test_new.py" in words:
            kind = "related"
        else:
            kind = "other"
        if kind == slow_check and not observed:
            assert timeout_s >= (stall_minutes + 1) * 60
            if kind == "related":
                assert timeout_s == 3600
            for _ in range(stall_minutes + 1):
                c.now[0] += timedelta(minutes=1)
                if polls:
                    polls[-1]()
                observed.append(c.store.get_run(1).last_progress_at)
            _recover_runs(scheduler, TickReport())
            assert killed == []
            assert c.store.get_run(1).status == "running"
            assert observed[-1] == c.now[0].isoformat()
        if report:
            suite = ET.Element("testsuite")
            ET.SubElement(suite, "testcase", file=baseline_file, name="test_keep",
                          classname=baseline_file.removesuffix(".py").replace("/", "."))
            ET.ElementTree(suite).write(report, encoding="unicode")
        return 0, False, ""

    monkeypatch.setattr("foragent.runner.poll_executor", watch)
    monkeypatch.setattr("foragent.runner.run_logged", check_process)
    assert run_issue(1, c.ctx) == "merged", c.store.log_path(1).read_text()
    assert len(set(observed)) == stall_minutes + 1
    assert killed == []


def test_check_timeout_uses_retry_limit_and_reports_command(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{"feature.txt": "done"}, {}]
    calls = []

    def check(command, worktree, log, timeout_s, **kwargs):
        calls.append((command, timeout_s))
        return CheckResult(command, -9, True, "slow check output")

    monkeypatch.setattr("foragent.runner._check", check)
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert calls == [(COMMAND, 900), (f"test -f {ACCEPTANCE}", 900)] * 2
    assert "slow check output" in c.executor.calls[1]["prompt"]
    assert "Check timed out." in c.tracker.list_comments(1)[-1].body
    assert c.tracker.created_prs == []


def test_retry_gets_remaining_budget_after_checks_spend_time(run_case, monkeypatch):
    c = run_case
    c.executor.steps = [{"feature.txt": "wrong"}, {"feature.txt": "done"}]
    from foragent.runner import _check

    def check(*args, **kwargs):
        c.now[0] += timedelta(minutes=1)
        return _check(*args, **kwargs)

    monkeypatch.setattr("foragent.runner._check", check)
    assert run_issue(1, c.ctx) == "merged"
    assert [call["timeout_s"] for call in c.executor.calls] == [90 * 60, 88 * 60]


def test_batch_acceptance_binding_still_uses_approval_snapshot(run_case):
    c = run_case
    batch_setup(c)
    seed_batch(c, {ACCEPTANCE: "weakened by sibling"})
    assert run_issue(1, c.ctx) == "hash-mismatch"
    assert c.store.load_flag(1).reason == Reason.HASH_MISMATCH
    assert not c.executor.calls and not c.tracker.created_prs


def test_batch_still_checks_base_acceptance_when_batch_has_old_approved_copy(run_case):
    c = run_case
    batch_setup(c)
    seed_batch(c, {"sibling.txt": "previous child"})
    c.history.update({ACCEPTANCE: "changed on base after approval"})
    assert run_issue(1, c.ctx) == "hash-mismatch"
    assert c.store.load_flag(1).reason == Reason.HASH_MISMATCH
    assert not c.executor.calls


@pytest.mark.parametrize("command,expected", [
    ("pytest -q --ignore=tests/acceptance", "pytest"),
    ("UV_CACHE_DIR='/a b' uv run --offline pytest -x tests", "UV_CACHE_DIR='/a b' uv run --offline pytest"),
    ("make test", None), ("pytest && rm -rf x", None), (None, None),
])
def test_fa_test_keeps_only_the_pytest_launcher(command, expected):
    from foragent.runner import _pytest_invocation

    assert _pytest_invocation(command) == expected


def test_fa_test_runs_pytest_in_a_slot_and_returns_its_exit_code(tmp_path, monkeypatch, capfd):
    from foragent import capacity
    from foragent.runner import run_tests

    binary = tmp_path / "bin/pytest"
    binary.parent.mkdir()
    binary.write_text(f"#!{sys.executable}\nimport sys\nprint(sys.argv[1:])\nraise SystemExit(3)\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    held = []
    original = capacity.hold_test_slot

    @contextmanager
    def observed(ceiling, **kwargs):
        with original(ceiling, **kwargs):
            held.append(ceiling)
            yield

    monkeypatch.setattr(capacity, "hold_test_slot", observed)
    assert run_tests(tmp_path, Config(test_command="pytest -q", test_slots=3), ["-k", "a b"]) == 3
    assert held == [3]
    assert "['-k', 'a b']" in capfd.readouterr().out


def test_check_tells_its_command_that_a_test_slot_is_held(tmp_path, monkeypatch):
    from foragent import capacity
    from foragent.runner import _check

    monkeypatch.delenv(capacity.TEST_SLOT_ENV, raising=False)
    log = tmp_path / "check.log"
    _check(f'echo "slot=${capacity.TEST_SLOT_ENV}"', tmp_path, log, 30, slot=capacity.hold_test_slot(1))
    assert "slot=held" in log.read_text()
    _check(f'echo "free=${capacity.TEST_SLOT_ENV}"', tmp_path, log, 30)
    assert "free=\n" in log.read_text()


def test_fa_test_inside_a_held_slot_runs_without_waiting(tmp_path, monkeypatch, capfd):
    from foragent import capacity
    from foragent.runner import run_tests

    binary = tmp_path / "bin/pytest"
    binary.parent.mkdir()
    binary.write_text(f"#!{sys.executable}\nimport os\nprint('child=' + os.environ.get({capacity.TEST_SLOT_ENV!r}, ''))\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv(capacity.TEST_SLOT_ENV, "held")
    with capacity.hold_test_slot(1):
        monkeypatch.setattr(capacity, "SLOT_POLL_SECONDS", 60)  # Any wait would hang the test.
        assert run_tests(tmp_path, Config(test_command="pytest", test_slots=1), []) == 0
    assert "child=held" in capfd.readouterr().out
