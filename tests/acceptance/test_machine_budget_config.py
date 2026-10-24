"""Acceptance test for machine-budget-config. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from conftest import git_command
from test_adaptive_budgets import sensor
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance
from test_test_slots import bind_cli, slot_case, tick_context


@pytest.fixture(autouse=True)
def machine_environment(monkeypatch, isolated_home, offline_acceptance):
    """Real machine files in temporary HOME; no OS jobs, signals or model turns."""
    from foragent import capacity, launchd
    from foragent.executors import ClaudeExecutor, CodexExecutor

    root = isolated_home / ".local/share/foragent"
    monkeypatch.setattr(capacity, "machine_root", lambda: root)

    def forbidden(*args, **kwargs):
        pytest.fail("machine-budget-config must not run OS jobs, signal processes or start models")

    def alive(pid, signum):
        assert pid in {880001, 880002} and signum == 0, (pid, signum)

    monkeypatch.setattr(os, "kill", alive)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(signal, "raise_signal", forbidden)
    monkeypatch.setattr(ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(CodexExecutor, "run", forbidden)
    monkeypatch.setattr(launchd, "_run_launchctl", forbidden)
    popen = subprocess.Popen

    def local_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and Path(argv[0]).name == "git", argv
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_git)
    return root


def machine_config(root, **values):
    """Public file format: a flat YAML mapping of the four machine budget keys."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "machine.yml").write_text(yaml.safe_dump(values), encoding="utf-8")


@pytest.fixture
def budget_repositories(git_history, tmp_path, monkeypatch, machine_environment, sensor):
    """Like capacity-aimd's machine_repositories, with real per-repo YAML inputs."""
    from foragent.config import load_config
    from foragent.state import StateStore

    def build(*settings):
        cases = []
        for index, values in enumerate(settings, 1):
            directory = tmp_path / f"repo-{index}"
            directory.mkdir()
            clone = directory / "clone"
            git_command(directory, "clone", str(git_history.origin), str(clone))
            repo = f"owner/repo-{index}"
            config = {
                "repo": repo, "max_parallel": 8, "test_slots": 4,
                "approvers": ["human"],
                "skill_libraries": [], "test_command": "true",
                "stall_minutes": 10000, "max_runtime_minutes": 10000,
                "lanes": {f"lane-{number}": {} for number in range(1, 9)},
                **values,
            }
            case = make_case(replace(git_history, clone=clone), directory, monkeypatch, **config)
            (clone / ".foragent.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
            case.config = case.ctx.config = load_config(clone)
            case.ctx.repo = repo
            case.store = case.ctx.store = StateStore.for_repo(repo, now=case.ctx.now)
            assert case.store.root.parent == machine_environment
            body = case.tracker.get_issue(1).body
            case.tracker.remove_labels(1, ["running", "approved"])
            for number in range(1, 9):
                if number != 1:
                    case.tracker.add_issue(number, f"Task {number}", body)
                case.tracker.add_labels(number, [f"lane:lane-{number}"])
                case.tracker.record_label(number, "approved", "human")
            case.launched = []

            def launch(number, case=case, pid=880000 + index):
                case.launched.append(number)
                return pid  # A fake live pid; no runner or executor starts.

            case.tick_ctx = replace(
                tick_context(case), repo=repo, launcher=launch,
                baseline_launcher=lambda *args, **kwargs: None,
            )
            cases.append(case)
        return SimpleNamespace(cases=cases, root=machine_environment, sensor=sensor)

    return build


def seed_budgets(scenario, *, slots, parallel):
    from foragent import capacity

    now = scenario.cases[0].clock[0]
    capacity.write_state(scenario.cases[0].store, parallel, now)
    (scenario.root / "test-slots.json").write_text(json.dumps({
        "slots": slots, "changed_at": now.isoformat(),
    }), encoding="utf-8")
    capacity.machine_load()  # Establish the swap-in counter using the existing sensor seams.


def tick_budgets(scenario, index, *, swapins=7):
    from foragent import capacity, scheduler

    # Pass both AIMD intervals; every tick observes its own genuine swap-in window.
    now = max(case.clock[0] for case in scenario.cases) + timedelta(minutes=10)
    for case in scenario.cases:
        case.clock[0] = now
    scenario.sensor.total += swapins
    case = scenario.cases[index]
    report = scheduler.tick(case.tick_ctx)
    assert report.skipped is None, report
    assert any("ready" in case.tracker.get_issue(number).labels for number in range(1, 9)), (
        "the shared worker budget must be exercised with approved work waiting"
    )
    assert all(item.implementer.calls == item.reviewer.calls == [] for item in scenario.cases)
    slots = json.loads((scenario.root / "test-slots.json").read_text(encoding="utf-8"))
    parallel = capacity.read_state(case.store)
    assert parallel is not None, "tick must persist the shared worker budget in capacity.json"
    return slots["slots"], parallel["parallel"]


def test_machine_budget_config_alternating_repositories_cannot_undo_machine_floors(
    budget_repositories, machine_environment,
):
    machine_config(machine_environment, test_slots=4, test_slots_floor=2,
                   parallel_floor=2, max_parallel=4)
    scenario = budget_repositories(
        {"test_slots_floor": 2, "parallel_floor": 2},
        {"test_slots_floor": 1, "parallel_floor": 1},
    )
    # Include a real transition so shared history is required without requiring
    # any particular policy for logging attempts clamped at the floor.
    seed_budgets(scenario, slots=3, parallel=3)
    actual = [tick_budgets(scenario, index) for index in (0, 1, 0, 1)]
    assert actual == [(2, 2)] * 4, (
        "kernel warn + swap-ins: alternating repo floors 2/1 must retain machine floors 2; "
        f"shared (test slots, workers) were {actual!r}"
    )
    path = scenario.root / "budget-history.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {row["budget"] for row in rows} == {"test_slots", "parallel"}
    assert all(row["to"] >= 2 and row["pressure"] == "warn" and row["swapins"] == 7 for row in rows)
    assert all(not (case.store.root / path.name).exists() for case in scenario.cases)


def test_machine_budget_config_repository_ceilings_only_limit_own_admission(
    budget_repositories, machine_environment,
):
    from foragent import capacity

    machine_config(machine_environment, test_slots=3, test_slots_floor=1,
                   parallel_floor=1, max_parallel=3)
    scenario = budget_repositories(
        {"test_slots": 1, "max_parallel": 1},
        {"test_slots": 8, "max_parallel": 8},
    )
    seed_budgets(scenario, slots=3, parallel=3)
    actual = []
    for index, case in enumerate(scenario.cases):
        actual.append((tick_budgets(scenario, index, swapins=0), len(case.launched)))
    assert actual == [((3, 3), 1), ((3, 3), 2)], (
        "repo ceilings 1/8 must leave both machine ceilings at 3, admitting one run in A "
        f"and only the two remaining machine slots in B; got {actual!r}"
    )
    assert capacity.effective_test_slots(1) == 1, "a repository may consume fewer test slots"
    assert capacity.effective_test_slots(8) == 3, "a repository cannot consume above machine capacity"


def test_machine_budget_config_repository_floors_cannot_raise_shared_budgets(
    budget_repositories, machine_environment,
):
    machine_config(machine_environment, test_slots=4, test_slots_floor=2,
                   parallel_floor=2, max_parallel=4)
    scenario = budget_repositories(
        {"test_slots_floor": 4, "parallel_floor": 4},
        {"test_slots_floor": 1, "parallel_floor": 1},
    )
    seed_budgets(scenario, slots=2, parallel=2)
    actual = [tick_budgets(scenario, index) for index in (0, 1)]
    assert actual == [(2, 2), (2, 2)], (
        f"a repo floor of 4 must not raise machine floors of 2 under warn pressure: {actual!r}"
    )


def test_machine_budget_config_critical_bypasses_floors_then_noncritical_restores_them(
    budget_repositories, machine_environment,
):
    machine_config(machine_environment, test_slots=4, test_slots_floor=3,
                   parallel_floor=3, max_parallel=6)
    scenario = budget_repositories({"test_slots_floor": 1, "parallel_floor": 1}, {})
    seed_budgets(scenario, slots=2, parallel=2)
    scenario.sensor.kernel = "critical"
    critical = tick_budgets(scenario, 0)
    scenario.sensor.kernel = "warn"
    recovered = tick_budgets(scenario, 1)
    assert (critical, recovered) == ((1, 1), (3, 3)), (
        "critical must bypass machine floors; the next noncritical tick must restore machine "
        f"floors despite lower repository floors: {(critical, recovered)!r}"
    )


@pytest.mark.parametrize("values", [
    pytest.param(None, id="no-machine-file"),
    pytest.param({"test_slots": 4}, id="partial-machine-file"),
    pytest.param({"test_slots": 4, "test_slots_floor": 2, "parallel_floor": 2,
                  "max_parallel": 6}, id="complete-machine-file"),
])
def test_machine_budget_config_status_reports_values_and_per_key_sources(
    git_history, tmp_path, monkeypatch, machine_environment, capsys, values,
):
    from foragent import cli
    from foragent.config import load_config

    defaults = {"test_slots": 2, "test_slots_floor": 1, "parallel_floor": 1, "max_parallel": None}
    if values is not None:
        machine_config(machine_environment, **values)
    case = slot_case(git_history, tmp_path, monkeypatch,
                     slots=2 if values is None else 9,
                     max_parallel=None if values is None else 9)
    main = bind_cli(case, monkeypatch)
    # Keep CLI isolation but let the real loader see repository and machine YAML.
    repository = {"repo": case.ctx.repo, "test_slots": case.config.test_slots, "skill_libraries": []}
    if values is not None:
        repository["max_parallel"] = 9
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(repository), encoding="utf-8")
    monkeypatch.setattr(cli, "load_config", load_config)
    assert main(["gate", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    expected = {
        key: {"value": (values or {}).get(key, default),
              "source": "machine" if key in (values or {}) else "default"}
        for key, default in defaults.items()
    }
    assert payload.get("machine_budget") == expected, (
        "gate status --json must expose machine_budget with {value, source} per key, "
        f"independent of repository settings; got {payload.get('machine_budget')!r}, expected {expected!r}"
    )
    assert payload["test_slots"] == expected["test_slots"]["value"]
