"""Resource measurements use owned lifetimes and step-local intervals, not host load."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psutil
import pytest

from foragent import capacity, orch_facts, step_weights
from foragent.config import Config
from foragent.state import Run, StateStore

NOW = datetime(2026, 11, 4, tzinfo=UTC)
GIB = 1024**3


@pytest.fixture
def measurement(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path / "machine")
    store = StateStore(tmp_path / "data" / "owner__project", now=lambda: NOW)
    rows = {100: [1., 10., 20., GIB], 101: [2., 40., 60., 3 * GIB]}

    class Process:
        def __init__(self, pid):
            if pid not in rows:
                raise psutil.NoSuchProcess(pid)
            self.pid = pid

        def create_time(self):
            return rows[self.pid][0]

        def status(self):
            return psutil.STATUS_RUNNING

        def cpu_times(self):
            # Descendant counters are intentionally large: never add these twice.
            return SimpleNamespace(user=rows[self.pid][1], system=rows[self.pid][2],
                                   children_user=99999, children_system=99999)

        def memory_info(self):
            return SimpleNamespace(rss=rows[self.pid][3])

    monkeypatch.setattr(step_weights.psutil, "Process", Process)
    run = Run(1, "branch", str(tmp_path / "work"), 100, NOW.isoformat(), NOW.isoformat(),
              phase="checking", phase_started_at=NOW.isoformat(),
              process_identities=[{"pid": pid, "start_time": row[0]} for pid, row in rows.items()])
    return SimpleNamespace(store=store, run=run, rows=rows, process=Process, root=tmp_path)


def learn(case):
    step_weights.record(case.store, [case.run], NOW)
    case.rows[100][1] += 5
    case.rows[101][2] += 15
    step_weights.record(case.store, [case.run], NOW + timedelta(seconds=10))
    return step_weights.learned(case.store)["checking"]


def test_tree_cpu_delta_and_summed_rss_are_exposed_in_frozen_facts(measurement, monkeypatch):
    c = measurement
    assert learn(c) == {"cpu_cores": 2., "memory_bytes": 4 * GIB,
                        "cpu_samples": 1, "memory_samples": 2,
                        "last_observed_at": (NOW + timedelta(seconds=10)).isoformat()}
    before = step_weights.path_for(c.store).read_bytes()
    monkeypatch.setattr(step_weights.psutil, "Process", lambda *a: pytest.fail("facts probed a process"))
    facts = orch_facts.collect(c.store, Config(), c.root)
    assert facts["step_weights"]["checking"]["cpu_cores"] == 2.
    assert "checking 2.00 CPU/4.00 GiB" in orch_facts.human(facts)
    assert step_weights.path_for(c.store).read_bytes() == before


def test_weights_survive_reload_without_runs_and_do_not_leak_to_another_project(measurement):
    c = measurement
    expected = learn(c)
    assert (expected["cpu_cores"], expected["memory_bytes"]) == (2., 4 * GIB)
    step_weights.record(c.store, [], NOW + timedelta(seconds=20))
    assert step_weights.learned(StateStore(c.store.root))["checking"] == expected
    other = StateStore(c.store.root.with_name("other__project"))
    assert step_weights.learned(other)["checking"]["cpu_cores"] is None


def test_reused_pid_never_contributes_its_cpu_or_memory(measurement):
    c = measurement
    step_weights.record(c.store, [c.run], NOW)
    c.rows[101] = [3., 999., 999., 100 * GIB]
    c.rows[100][1] += 2
    step_weights.record(c.store, [c.run], NOW + timedelta(seconds=10))
    weight = step_weights.learned(c.store)["checking"]
    assert weight["cpu_cores"] is None and weight["cpu_samples"] == 0
    assert weight["memory_bytes"] == 2.5 * GIB


def test_phase_transition_does_not_charge_previous_step_cpu_to_new_step(measurement):
    c = measurement
    step_weights.record(c.store, [c.run], NOW)
    c.rows[101][1] += 1000
    review = replace(c.run, phase="reviewing", phase_started_at=(NOW + timedelta(seconds=10)).isoformat())
    step_weights.record(c.store, [review], NOW + timedelta(seconds=10))
    assert step_weights.learned(c.store)["reviewing"]["cpu_cores"] is None
    c.rows[100][1] += 1
    step_weights.record(c.store, [review], NOW + timedelta(seconds=20))
    assert step_weights.learned(c.store)["reviewing"]["cpu_cores"] == pytest.approx(.1)


def test_new_child_cpu_is_counted_from_birth_within_the_interval(measurement):
    c = measurement
    c.run = replace(c.run, process_identities=c.run.process_identities[:1])
    step_weights.record(c.store, [c.run], NOW)
    birth = NOW.timestamp() + 5
    c.rows[102] = [birth, 8., 2., 2 * GIB]
    run = replace(c.run, process_identities=[*c.run.process_identities, {"pid": 102, "start_time": birth}])
    step_weights.record(c.store, [run], NOW + timedelta(seconds=10))
    assert step_weights.learned(c.store)["checking"]["cpu_cores"] == 1.


def test_unreadable_child_does_not_teach_a_partial_tree_or_bridge_a_gap(measurement, monkeypatch):
    c = measurement
    expected = learn(c)
    real = c.process.memory_info

    def denied(process):
        if process.pid == 101:
            raise psutil.AccessDenied(101)
        return real(process)

    with monkeypatch.context() as patch:
        patch.setattr(c.process, "memory_info", denied)
        step_weights.record(c.store, [c.run], NOW + timedelta(seconds=20))
    step_weights.record(c.store, [c.run], NOW + timedelta(seconds=30))
    weight = step_weights.learned(c.store)["checking"]
    assert weight["cpu_cores"] == expected["cpu_cores"]
    assert weight["cpu_samples"] == 1 and weight["memory_samples"] == 3


def test_repeated_clock_does_not_duplicate_observations(measurement):
    c = measurement
    expected = learn(c)
    for _ in range(3):
        step_weights.record(c.store, [c.run], NOW + timedelta(seconds=10))
    assert step_weights.learned(c.store)["checking"] == expected


def test_empty_and_unknown_phases_do_not_invent_measurements(measurement):
    c = measurement
    step_weights.record(c.store, [replace(c.run, phase="test-slot-wait")], NOW)
    assert not step_weights.path_for(c.store).exists()
    assert all(weight["cpu_cores"] is None for weight in step_weights.learned(c.store).values())


def test_write_failure_preserves_last_learned_file(measurement, monkeypatch):
    c = measurement
    learn(c)
    before = step_weights.path_for(c.store).read_bytes()
    monkeypatch.setattr(step_weights.os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError()))
    step_weights.record(c.store, [c.run], NOW + timedelta(seconds=20))
    assert step_weights.path_for(c.store).read_bytes() == before
    assert list(step_weights.path_for(c.store).parent.glob(".step-weights-*")) == []


def test_launch_birth_is_pinned_for_measurement_without_granting_stop_ownership(measurement, monkeypatch):
    c = measurement
    c.rows[100][0], c.rows[101][0] = NOW.timestamp(), NOW.timestamp() + 1
    run = replace(c.run, process_identities=[])
    step_weights.started(c.store, run)
    root = {"pid": 100, "start_time": NOW.timestamp()}
    child = {"pid": 101, "start_time": NOW.timestamp() + 1}
    witnessed = []

    def observe(identities):
        witnessed.append(identities)
        return [root, child]

    monkeypatch.setattr(step_weights.processes, "observe_routine_tree", observe)
    step_weights.record(c.store, [run], NOW + timedelta(seconds=5))
    c.rows[101][1] += 10
    step_weights.record(c.store, [run], NOW + timedelta(seconds=10))
    assert witnessed == [[root], [root, child]]
    assert step_weights.learned(c.store)["checking"]["cpu_cores"] == 2.
    assert step_weights.learned(c.store)["checking"]["memory_bytes"] == 4 * GIB
    assert run.process_identities == []


def test_new_execution_does_not_inherit_a_prior_launchs_tree(measurement, monkeypatch):
    c = measurement
    step_weights.started(c.store, c.run)
    new = replace(c.run, started_at=(NOW + timedelta(seconds=10)).isoformat(), process_identities=[])
    monkeypatch.setattr(step_weights.processes, "observe_routine_tree",
                        lambda *a: pytest.fail("inherited a different execution's root"))
    step_weights.record(c.store, [new], NOW + timedelta(seconds=20))
    assert step_weights.learned(c.store)["checking"]["memory_bytes"] is None
