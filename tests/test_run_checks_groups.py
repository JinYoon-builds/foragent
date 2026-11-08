"""Related groups preserve serial check inputs, timing and failure semantics."""

from contextlib import contextmanager
from types import SimpleNamespace
import threading
import sys

import pytest

from foragent import capacity, run_checks, run_checks_groups, runner
from foragent.config import Config


@pytest.mark.parametrize("groups", [1, 2])
@pytest.mark.parametrize("kind,expected", [("empty-paths", 0), ("empty-file", 0), ("all-empty", 0),
                                         ("failure", 1), ("syntax", 2)])
def test_related_empty_selections_preserve_real_failures(tmp_path, monkeypatch, groups, kind, expected):
    from conftest import BASE_TIME
    from foragent.git import GitRepo
    from foragent.runner import RunContext
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    store.start_run(1, branch="test", worktree=root)
    ctx = RunContext("owner/repo", root, FakeTracker(), store, Config(related_check_groups=groups),
                     GitRepo(root), lambda: BASE_TIME)
    (root / "tests/test_empty.py").write_text("# helpers only\n")
    (root / "tests/test_open.py").write_text(
        "# no tests\n" if kind == "all-empty" else
        "def test_broken(:\n" if kind == "syntax" else f"def test_open(): assert {kind != 'failure'}\n")
    paths = [] if kind == "empty-paths" else ["tests/test_empty.py", "tests/test_open.py"]
    result = run_checks.run_related_check(1, ctx, root, run_checks._pytest_command(f"{sys.executable} -m pytest"),
                                           paths, 1, 120)
    assert result.exit_code == expected, result.output
    assert len(store.get_run(1).related_tests) == 1
    assert store.get_run(1).related_tests[0]["selected_files"] == sorted(paths)


@pytest.mark.parametrize("count, files, expected", [
    (1, ["a", "b"], 1),
    (2, ["a"], 1),
    (2, ["a", "b", "c"], 2),
    (5, ["a", "b"], 2),
])
def test_related_check_group_count_honors_setting_and_selection(count, files, expected):
    ctx = SimpleNamespace(config=Config(related_check_groups=count))
    assert run_checks_groups.group_count(ctx, files) == expected


@pytest.mark.parametrize("seconds, expected", [
    ({"a": 10, "b": 8, "c": 3, "d": 1}, [["a", "d"], ["b", "c"]]),
    ({"a": 10, "b": 2}, [["a", "b"], ["c", "d"]]),
    ({}, [["a", "c"], ["b", "d"]]),
    ({"a": 0, "b": 0, "c": 0, "d": 0}, [["a", "c"], ["b", "d"]]),
])
def test_related_check_split_balances_recorded_seconds_with_median_for_missing(seconds, expected):
    assert run_checks_groups.split(["a", "b", "c", "d"], seconds, 2) == expected


def test_related_check_uses_latest_recorded_file_seconds_including_retries():
    history = [
        SimpleNamespace(related_tests=[{"file_seconds": {"a": 10, "b": 2}}]),
        SimpleNamespace(related_tests=[{"file_seconds": {"a": 8}}]),
    ]
    current = SimpleNamespace(related_tests=[{"file_seconds": {"b": 3}}, {"file_seconds": {"b": 1}}])
    ctx = SimpleNamespace(store=SimpleNamespace(read_history=lambda: history, get_run=lambda n: current))
    assert run_checks_groups.recorded_seconds(ctx, 1) == {"a": 8, "b": 1}


@pytest.mark.parametrize("exit_code, timed_out", [(0, False), (1, False), (-9, True)])
def test_related_check_groups_share_environment_and_merge_wall_time_and_failures(
        tmp_path, monkeypatch, exit_code, timed_out):
    commands = ["pytest tests/test_a.py", "pytest tests/test_b.py"]
    started = threading.Barrier(2, timeout=5)
    finished = threading.Barrier(2, timeout=5)
    clock = [100.0]
    environments = []
    active = set()
    lock = threading.Lock()

    @contextmanager
    def slot():
        token = object()
        with lock:
            active.add(token)
        try:
            yield
        finally:
            with lock:
                active.remove(token)

    def process(argv, *, env, **kwargs):
        with lock:
            environments.append(env)
        if started.wait() == 0:
            clock[0] += 9
        assert len(active) == 2
        finished.wait()
        return (exit_code, timed_out, "") if argv[2] == commands[1] else (0, False, "")

    monkeypatch.setattr(runner, "run_logged", process)
    ctx = SimpleNamespace(monotonic=lambda: clock[0])
    token = run_checks.CHECK_ENVIRONMENT.set({"PYTEST_ADDOPTS": "--existing-check-options"})
    try:
        result = run_checks_groups.run_groups(
            1, ctx, tmp_path, commands, 60, tmp_path / "check.log", tmp_path, slot)
    finally:
        run_checks.CHECK_ENVIRONMENT.reset(token)

    assert result.exit_code == exit_code
    assert result.timed_out is timed_out
    assert result.seconds == 9
    assert result.command.splitlines() == commands
    assert not active
    assert len(environments) == 2
    for environment in environments:
        assert environment["PYTEST_ADDOPTS"] == "--existing-check-options"
        assert environment[capacity.TEST_SLOT_ENV] == "held"
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"


def test_holding_keeps_files_with_selected_ids_and_falls_back_when_unmapped():
    from foragent import run_checks_groups as groups

    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]
    assert groups.holding(files, ["tests/test_c.py::t", "tests/test_a.py::k[1]"]) == (
        ["tests/test_a.py", "tests/test_c.py"], ["tests/test_b.py"])
    assert groups.holding(files, []) is None
    assert groups.holding(files, ["tests/other.py::t"]) is None
