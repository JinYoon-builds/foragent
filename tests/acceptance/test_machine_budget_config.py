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
from test_adaptive_budgets import sample_clock, sensor
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
    """Active ceilings plus optional, ignored legacy floor keys in a flat YAML mapping."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "machine.yml").write_text(yaml.safe_dump(values), encoding="utf-8")


@pytest.fixture
def budget_repositories(git_history, tmp_path, monkeypatch, machine_environment, sensor, sample_clock):
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
            # Legacy keys exercise the public YAML loader, not Config's constructor.
            active = {key: value for key, value in config.items()
                      if key not in {"test_slots_floor", "parallel_floor"}}
            case = make_case(replace(git_history, clone=clone), directory, monkeypatch, **active)
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
        sample_clock(cases[0].clock)
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


def tick_budgets(scenario, index, *, swapins=0, seconds=30):
    from foragent import capacity, scheduler

    now = max(case.clock[0] for case in scenario.cases) + timedelta(seconds=seconds)
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
    # memory-backoff: legacy repo floors cannot interrupt a machine-wide streak.
    machine_config(machine_environment, test_slots=4, max_parallel=4)
    scenario = budget_repositories(
        {"test_slots_floor": 2, "parallel_floor": 2},
        {"test_slots_floor": 1, "parallel_floor": 1},
    )
    seed_budgets(scenario, slots=3, parallel=3)
    actual = [tick_budgets(scenario, index, swapins=101) for index in (0, 1, 0)]
    assert actual == [(3, 3), (3, 3), (2, 1)], (
        "three hot windows across repositories must back off below ignored legacy floors; "
        f"shared (test slots, workers) were {actual!r}"
    )
    path = scenario.root / "budget-history.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {row["budget"] for row in rows} == {"test_slots", "parallel"}
    assert {(row["budget"], row["from"], row["to"]) for row in rows} == {
        ("test_slots", 3, 2), ("parallel", 3, 1),
    }
    assert all(row["pressure"] == "warn" and row["swapins"] == 101 for row in rows)
    assert all(not (case.store.root / path.name).exists() for case in scenario.cases)


def test_machine_budget_config_repository_ceilings_only_limit_own_admission(
    budget_repositories, machine_environment,
):
    # memory-headroom: a repository may lower only its own admission ceiling.
    from foragent import capacity

    machine_config(machine_environment, test_slots=3, max_parallel=3)
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
    # memory-backoff: accept legacy floors in both YAML layers and ignore them.
    machine_config(machine_environment, test_slots=4, test_slots_floor=2,
                   parallel_floor=2, max_parallel=4)
    scenario = budget_repositories(
        {"test_slots_floor": 4, "parallel_floor": 4},
        {"test_slots_floor": 1, "parallel_floor": 1},
    )
    seed_budgets(scenario, slots=2, parallel=2)
    actual = [tick_budgets(scenario, index, swapins=101) for index in (0, 1, 0)]
    assert actual == [(2, 2), (2, 2), (1, 1)], (
        f"legacy repo/machine floors must not raise or clamp the shared backoff: {actual!r}"
    )


def test_machine_budget_config_critical_bypasses_floors_then_noncritical_restores_them(
    budget_repositories, machine_environment,
):
    # memory-backoff: critical waits five minutes; recovery adds toward ceilings.
    from foragent import capacity

    machine_config(machine_environment, test_slots=4, max_parallel=6)
    scenario = budget_repositories({"test_slots_floor": 1, "parallel_floor": 1}, {})
    seed_budgets(scenario, slots=4, parallel=6)
    scenario.sensor.kernel = "critical"
    critical = [tick_budgets(scenario, 0, seconds=0)]
    critical += [tick_budgets(scenario, index % 2) for index in range(10)]
    assert critical == [(4, 6)] * 10 + [(3, 3)], (
        f"critical must wait for five continuous minutes before decreasing: {critical!r}"
    )
    scenario.sensor.kernel = "normal"
    recovered = [tick_budgets(scenario, index % 2,
                             seconds=capacity.INCREASE_INTERVAL.total_seconds() + 30)
                 for index in range(3)]
    assert recovered == [(4, 4), (4, 5), (4, 6)], (
        f"without floor keys both budgets must recover to machine ceilings: {recovered!r}"
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

    # memory-backoff: status reports active ceilings; accepted legacy floors vanish.
    defaults = {"test_slots": 2, "max_parallel": None}
    if values is not None:
        machine_config(machine_environment, **values)
    case = slot_case(git_history, tmp_path, monkeypatch,
                     slots=2 if values is None else 9,
                     max_parallel=None if values is None else 9)
    main = bind_cli(case, monkeypatch)
    # Keep CLI isolation but let the real loader see repository and machine YAML.
    repository = {"repo": case.ctx.repo, "test_slots": case.config.test_slots, "skill_libraries": [],
                  "test_slots_floor": 4, "parallel_floor": 4}
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
