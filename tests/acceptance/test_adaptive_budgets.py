"""Acceptance test for adaptive-budgets. Locked by Foragent; do not edit."""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance
from test_test_slots import bind_cli, machine_yml, slot_case, tick_context


@pytest.fixture(autouse=True)
def adaptive_environment(monkeypatch, isolated_home, offline_acceptance):
    """Use the actual machine directory layout, entirely inside the fake HOME."""
    from foragent import capacity
    from foragent.executors import ClaudeExecutor, CodexExecutor

    root = isolated_home / ".local/share/foragent"
    monkeypatch.setattr(capacity, "machine_root", lambda: root)

    def forbidden(*args, **kwargs):
        pytest.fail("adaptive-budget tests must not signal processes or start model turns")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(CodexExecutor, "run", forbidden)
    return root


@pytest.fixture
def sensor(monkeypatch):
    from foragent import capacity

    sample = SimpleNamespace(kernel="warn", total=100, load=0.5, kernel_reads=[], swapin_reads=[])

    def kernel_pressure():
        sample.kernel_reads.append(sample.kernel)
        return sample.kernel

    def swapins_total():
        sample.swapin_reads.append(sample.total)
        return sample.total

    # Supply proposed seams before they exist. Keep the old direct-kernel path
    # deterministic too, so baseline failures describe classification, not the host.
    monkeypatch.setattr(capacity, "kernel_pressure", kernel_pressure, raising=False)
    monkeypatch.setattr(capacity, "swapins_total", swapins_total, raising=False)
    monkeypatch.setattr(capacity, "memory_pressure", lambda: sample.kernel, raising=False)
    monkeypatch.setattr(capacity, "load_per_cpu", lambda: sample.load)
    return sample


@pytest.mark.parametrize("samples", [
    pytest.param([
        ("warn", 100, "warn"), ("warn", 100, "normal"), ("warn", 100, "normal"),
    ], id="compression-without-swapins"),
    pytest.param([
        ("normal", 100, "normal"), ("normal", 101, "warn"),
        ("normal", 101, "normal"), ("normal", 108, "warn"),
        ("warn", 108, "normal"),
    ], id="swapins-override-normal-for-one-window"),
    pytest.param([
        ("critical", None, "critical"), ("critical", 100, "critical"),
        ("critical", 101, "critical"), ("critical", 101, "critical"),
        ("warn", 101, "normal"),
    ], id="critical-always-wins"),
    pytest.param([
        ("warn", None, "warn"), ("warn", None, "warn"),
        ("warn", 100, "warn"), ("warn", 100, "normal"),
        ("warn", None, "warn"),
    ], id="unknown-is-conservative"),
    pytest.param([
        ("warn", 100, "warn"), ("warn", 2, "normal"), ("warn", 2, "normal"),
        ("normal", 3, "warn"), ("warn", 3, "normal"),
    ], id="counter-reset-rebases-the-window"),
])
def test_adaptive_budgets_sensor_classifies_sample_windows(sensor, samples):
    from foragent import capacity

    actual = []
    for kernel, total, _ in samples:
        sensor.kernel, sensor.total = kernel, total
        actual.append(capacity.machine_load())
    expected = [(pressure, sensor.load) for _, _, pressure in samples]
    assert actual == expected, f"machine_load must classify swap-in windows: {actual!r} != {expected!r}"
    assert sensor.kernel_reads and sensor.swapin_reads, "machine_load must consume both public seams"


def test_adaptive_budgets_raw_sensors_are_public_and_in_process(monkeypatch):
    from foragent import capacity

    kernel = getattr(capacity, "kernel_pressure", None)
    swapins = getattr(capacity, "swapins_total", None)
    assert callable(kernel), "capacity.kernel_pressure() must expose the raw OS pressure level"
    assert callable(swapins), "capacity.swapins_total() must expose cumulative swap-in pages"

    def no_subprocess(*args, **kwargs):
        pytest.fail("raw memory sensors must read in-process, without a subprocess")

    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    monkeypatch.setattr(subprocess, "run", no_subprocess)
    assert kernel() in {"normal", "warn", "critical"}
    total = swapins()
    assert total is None or (type(total) is int and total >= 0), total


def test_adaptive_budgets_sample_counter_survives_fresh_processes(sensor, isolated_home):
    from foragent import capacity

    assert capacity.machine_load() == ("warn", 0.5)
    # Copy only machine data into another isolated HOME. A module cache or a
    # counter saved elsewhere in HOME cannot satisfy persistence this way.
    fresh_home = isolated_home / "fresh-process-home"
    fresh_root = fresh_home / ".local/share/foragent"
    fresh_root.mkdir(parents=True)
    root = capacity.machine_root()
    if root.exists():
        shutil.copytree(root, fresh_root, dirs_exist_ok=True)
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import json, os, subprocess, sys
from foragent import capacity
def forbidden(*args, **kwargs):
    raise AssertionError("sensor subprocess must not signal or spawn processes")
os.kill = os.killpg = subprocess.Popen = subprocess.run = forbidden
capacity.kernel_pressure = lambda: "warn"
capacity.memory_pressure = lambda: "warn"
capacity.swapins_total = lambda: int(sys.argv[1])
capacity.load_per_cpu = lambda: 0.5
print(json.dumps(capacity.machine_load()))
"""

    def fresh_sample(total):
        result = subprocess.run(
            [sys.executable, "-c", script, str(total)], check=True, capture_output=True, text=True,
            env={
                **os.environ, "HOME": str(fresh_home), "PYTHONPATH": str(source),
                "XDG_DATA_HOME": str(fresh_home / ".local/share"),
                "XDG_CONFIG_HOME": str(fresh_home / ".config"),
                "XDG_CACHE_HOME": str(fresh_home / ".cache"),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        return tuple(json.loads(result.stdout))

    actual = [fresh_sample(total) for total in (100, 107, 107)]
    assert actual == [("normal", 0.5), ("warn", 0.5), ("normal", 0.5)], (
        f"previous swap-in counter must persist in the machine data directory across processes: {actual!r}"
    )


@pytest.fixture
def adaptive_case(git_history, tmp_path, monkeypatch, adaptive_environment, sensor):
    case = slot_case(git_history, tmp_path, monkeypatch, slots=3)
    machine_yml(slots=3, max_parallel=8)  # Machine-wide budgets come from machine.yml (#365).
    main = bind_cli(case, monkeypatch)
    context = tick_context(case, start_runs=False)
    root = adaptive_environment
    root.mkdir(parents=True, exist_ok=True)
    # Reproduce the machine stuck at one slot; the ceiling remains three.
    (root / "test-slots.json").write_text(json.dumps({
        "slots": 1, "changed_at": (case.clock[0] - timedelta(minutes=10)).isoformat(),
    }), encoding="utf-8")
    return SimpleNamespace(case=case, main=main, context=context, root=root, sensor=sensor)


def tick_slots(scenario, capsys):
    from foragent import scheduler

    scenario.case.clock[0] += timedelta(minutes=10)
    report = scheduler.tick(scenario.context)
    assert report.skipped is None, report
    assert scenario.main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    slots = payload.get("test_slots")
    assert type(slots) is int, f"gate status must expose an integer test_slots: {payload!r}"
    return slots


def test_adaptive_budgets_tick_recovers_slots_then_decreases_on_swapins(adaptive_case, capsys):
    from foragent import capacity

    scenario = adaptive_case
    capacity.machine_load()  # Establish the previous counter without a budget update.
    actual = [tick_slots(scenario, capsys) for _ in range(3)]
    scenario.sensor.total += 7
    actual.append(tick_slots(scenario, capsys))
    assert actual == [2, 3, 3, 2], (
        f"kernel warn without swap-ins must recover to the ceiling, then swap-ins remove one slot: {actual!r}"
    )
    assert scenario.sensor.kernel_reads and scenario.sensor.swapin_reads, (
        "tick must consume capacity's public raw sensors through the real machine_load"
    )


HISTORY_FIELDS = ("at", "budget", "from", "to", "pressure", "load_per_cpu", "swapins")


def history(root):
    path = root / "budget-history.jsonl"
    assert path.is_file(), "budget changes must append machine-wide budget-history.jsonl"
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n"), "each budget history record must be a complete JSON line"
    rows = [json.loads(line) for line in text.splitlines()]
    for row in rows:
        assert set(HISTORY_FIELDS) <= row.keys(), row
        assert isinstance(row["at"], str), row
        datetime.fromisoformat(row["at"])
        assert row["budget"] in {"test_slots", "parallel"}, row
        assert type(row["from"]) is int and type(row["to"]) is int, row
        assert row["from"] != row["to"], "unchanged budgets must not produce history entries"
        assert row["pressure"] in {"normal", "warn", "critical"}, row
        assert isinstance(row["load_per_cpu"], (int, float)), row
        assert row["swapins"] is None or (type(row["swapins"]) is int and row["swapins"] >= 0), row
    return rows


def assert_history(root, expected):
    rows = history(root)
    actual = [tuple(row[field] for field in HISTORY_FIELDS[1:]) for row in rows]
    assert actual == expected, f"budget history must preserve every transition and its sensor window: {actual!r}"


def test_adaptive_budgets_slot_history_appends_changes_and_omits_noops(adaptive_case, capsys):
    from foragent import capacity

    scenario = adaptive_case
    capacity.machine_load()
    expected = []
    for before, after in ((1, 2), (2, 3)):
        tick_slots(scenario, capsys)
        expected.append(("test_slots", before, after, "normal", 0.5, 0))
        assert_history(scenario.root, expected)

    path = scenario.root / "budget-history.jsonl"
    unchanged = path.read_bytes()
    tick_slots(scenario, capsys)  # Already at the configured ceiling.
    assert path.read_bytes() == unchanged, "a tick with no budget change must append nothing"

    scenario.sensor.total += 7
    tick_slots(scenario, capsys)
    expected.append(("test_slots", 3, 2, "warn", 0.5, 7))
    assert_history(scenario.root, expected)


def test_adaptive_budgets_slot_history_records_unknown_swapins_as_null(adaptive_case, capsys):
    scenario = adaptive_case
    (scenario.root / "test-slots.json").write_text(json.dumps({
        "slots": 3, "changed_at": scenario.case.clock[0].isoformat(),
    }), encoding="utf-8")
    scenario.sensor.total = None
    assert tick_slots(scenario, capsys) == 2
    assert_history(scenario.root, [
        ("test_slots", 3, 2, "warn", 0.5, None),
    ])


def test_adaptive_budgets_parallel_history_uses_machine_budget_path(
    git_history, tmp_path, monkeypatch, adaptive_environment, sensor,
):
    from foragent import capacity, scheduler
    from foragent.state import StateStore
    from test_orch_review_handoff import make_case

    # Use the same public tick setup as capacity-aimd's machine_repositories,
    # retaining the real machine_load and pinning test slots at one throughout.
    case = make_case(
        git_history, tmp_path, monkeypatch, max_parallel=8, test_slots=1,
        skill_libraries=[], test_command="true",
        lanes={f"lane-{number}": {} for number in range(1, 9)},
    )
    case.store = case.ctx.store = StateStore.for_repo(case.ctx.repo, now=case.ctx.now)
    assert case.store.root.parent == adaptive_environment
    machine_yml(slots=1, max_parallel=8)  # Machine-wide budgets come from machine.yml (#365).
    body = case.tracker.get_issue(1).body
    case.tracker.remove_labels(1, ["running", "approved"])
    for number in range(1, 9):
        if number != 1:
            case.tracker.add_issue(number, f"Task {number}", body)
        case.tracker.add_labels(number, [f"lane:lane-{number}"])
        case.tracker.record_label(number, "approved", "human")

    launched = []
    live_pid = os.getpid()

    def launch(number):
        launched.append(number)
        return live_pid  # Record a live pid without starting a worker or model.

    def process_exists(pid, signal):
        # Even signal 0 must stay inside this fake boundary; never call os.kill.
        assert (pid, signal) == (live_pid, 0), "only the fake liveness probe is allowed"

    def no_kill(pid):
        pytest.fail("changing a machine budget must not kill a running worker")

    monkeypatch.setattr(os, "kill", process_exists)
    context = tick_context(case)
    context.launcher, context.kill_runner = launch, no_kill
    assert context.start_runs

    capacity.write_state(case.store, 2, case.clock[0])
    (adaptive_environment / "test-slots.json").write_text(json.dumps({
        "slots": 1, "changed_at": case.clock[0].isoformat(),
    }), encoding="utf-8")
    capacity.machine_load()

    case.clock[0] += capacity.INCREASE_INTERVAL + timedelta(seconds=1)
    assert scheduler.tick(context).skipped is None
    state = capacity.read_state(case.store)
    assert state is not None and state["parallel"] == 3, (
        f"kernel warn with zero swap-ins must grow parallel from 2 to 3 after the interval: {state!r}"
    )
    assert len(launched) == 3
    assert all(run.pid == live_pid for run in case.store.active_runs())
    assert any("ready" in case.tracker.get_issue(number).labels for number in range(1, 9))
    expected = [("parallel", 2, 3, "normal", 0.5, 0)]
    assert_history(adaptive_environment, expected)

    # The very next sensor sample has swap-ins: decrease immediately, even
    # though less than an increase interval has elapsed since the growth.
    sensor.total += 7
    case.clock[0] += timedelta(seconds=1)
    assert scheduler.tick(context).skipped is None
    state = capacity.read_state(case.store)
    assert state is not None and state["parallel"] == 1, (
        f"the next swap-in window must halve parallel immediately from 3 to 1: {state!r}"
    )
    assert len(launched) == len(case.store.active_runs()) == 3
    expected.append(("parallel", 3, 1, "warn", 0.5, 7))
    assert_history(adaptive_environment, expected)

    path = adaptive_environment / "budget-history.jsonl"
    unchanged = path.read_bytes()
    assert scheduler.tick(context).skipped is None  # Increase interval has not elapsed.
    assert capacity.read_state(case.store) == state
    assert path.read_bytes() == unchanged, "an unchanged parallel budget must append nothing"

    case.clock[0] += capacity.INCREASE_INTERVAL + timedelta(seconds=1)
    assert scheduler.tick(context).skipped is None
    assert capacity.read_state(case.store)["parallel"] == 2
    expected.append(("parallel", 1, 2, "normal", 0.5, 0))
    assert_history(adaptive_environment, expected)

    case.clock[0] += timedelta(seconds=1)
    sensor.kernel, sensor.total = "critical", None
    assert scheduler.tick(context).skipped is None
    assert capacity.read_state(case.store)["parallel"] == 1
    expected.append(("parallel", 2, 1, "critical", 0.5, None))
    assert_history(adaptive_environment, expected)
    assert case.implementer.calls == case.reviewer.calls == []
