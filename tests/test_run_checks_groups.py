"""Related groups preserve serial check inputs, timing and failure semantics."""

from contextlib import contextmanager
from types import SimpleNamespace
import threading

import pytest

from foragent import capacity, run_checks, run_checks_groups, runner
from foragent.config import Config


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
