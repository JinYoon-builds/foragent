"""Order: load-tick-architect-park; public session boundaries, no live agents."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import capacity, executors, order, order_perspectives, orch_architect, scheduler
from foragent.config import Config
from foragent.executors import ExecResult
from foragent.state import StateStore
from test_orch_review_handoff import make_case, tick_context


@pytest.fixture(autouse=True)
def fake_workloads(fake_process_table, monkeypatch):
    """Every stored workload PID belongs only to the fake table; never signal it."""
    monkeypatch.setattr(orch_architect, "os", capacity.os)
    yield fake_process_table
    assert fake_process_table.signals == [], "Capacity accounting must not signal any process"


@pytest.fixture
def planning(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    config = Config(repo="owner/project", executor="codex", language="en", skill_libraries=[])
    store = StateStore(tmp_path / "machine" / "owner__project")
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: store))
    monkeypatch.setattr(capacity, "machine_root", lambda: store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.5))
    capacity.write_state(store, 1, datetime.now(UTC))
    plan = {"id": "budget", "title": "Budget", "summary": "Share machine slots",
            "spec_markdown": "# Budget\n", "issues": [
                {"key": "one", "title": "One", "goal": "Read", "out_of_scope": "Implement",
                 "done_when": ["true"], "lane": "main", "size": "s"}]}
    monkeypatch.setattr(order_perspectives, "seat_executors",
                        lambda config: {seat: "codex" for seat in order_perspectives.SEATS})
    case = SimpleNamespace(root=root, config=config, store=store, plan=plan, observations=[])

    def answer(**kwargs):
        case.observations.append(capacity.machine_runs(store, datetime.now(UTC)))
        return ExecResult(0, False, "NONE", kwargs["log_path"])

    case.answer = answer

    def run(**kwargs):
        return case.answer(**kwargs)

    factory = lambda name: SimpleNamespace(run=run)
    monkeypatch.setattr(executors, "executor_for", factory)
    monkeypatch.setattr(order, "executor_for", factory)
    return case


def invoke(case, command):
    if command == "preread":
        assert order.preread(case.plan, case.root, case.config) == [{"key": "one", "unknowns": []}]
        return 1
    result = order_perspectives.run(case.plan, case.root, case.config, case.store)
    assert result["failures"] == [], result
    return len(order_perspectives.SEATS)


@pytest.mark.parametrize("command", ["preread", "perspectives"])
def test_each_session_occupies_exactly_one_slot_and_releases_it(planning, command):
    count = invoke(planning, command)
    assert planning.observations == [1] * count
    assert capacity.machine_runs(planning.store, datetime.now(UTC)) == 0


@pytest.mark.parametrize("command", ["preread", "perspectives"])
@pytest.mark.parametrize("reason", ["full", "overload"])
def test_waits_for_existing_budget_or_sensor_then_runs_once(planning, command, reason, monkeypatch, capsys):
    case = planning
    sibling = StateStore(case.store.root.parent / "other")
    overloaded = [reason == "overload"]
    if reason == "full":
        sibling.start_run(99, branch="other", worktree=sibling.worktree_path(99), pid=capacity.os.getpid())
    sample = lambda: ("normal", capacity.OVERLOAD_LOAD if overloaded[0] else 0.5)
    monkeypatch.setattr(capacity, "machine_load", sample)
    monkeypatch.setattr(scheduler, "machine_load", sample)
    sleeps = []

    def release(seconds):
        assert not case.observations, "The agent started before capacity became available"
        sleeps.append(seconds)
        overloaded[0] = False
        if reason == "full" and sibling.active_runs():
            sibling.finish_run(99, "merged")

    monkeypatch.setattr(capacity.time, "sleep", release)
    count = invoke(case, command)
    assert sleeps, "No wait occurred while admission was blocked"
    assert case.observations == [1] * count
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).lower().splitlines()
    assert any("wait" in line and ("budget" in line or "slot" in line or "load" in line) for line in lines)
    assert capacity.machine_runs(case.store, datetime.now(UTC)) == 0


def test_preread_gives_up_after_ten_minutes_without_starting_or_caching_success(planning, monkeypatch, capsys):
    case = planning
    sibling = StateStore(case.store.root.parent / "other")
    sibling.start_run(99, branch="other", worktree=sibling.worktree_path(99), pid=capacity.os.getpid())
    elapsed = [0.0]
    monkeypatch.setattr(capacity.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(capacity.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + 600))
    with pytest.raises(order.PlanError, match="(?i)(budget|slot|capacity)"):
        order.preread(case.plan, case.root, case.config)
    assert 600 <= elapsed[0] <= 601
    assert case.observations == []
    assert not (case.root / order.PREREAD_FILE).exists()
    captured = capsys.readouterr()
    assert "wait" in (captured.out + captured.err).lower()


@pytest.mark.parametrize("command", ["preread", "perspectives"])
def test_executor_failure_releases_its_slot(planning, command):
    case = planning

    def fail(**kwargs):
        case.observations.append(capacity.machine_runs(case.store, datetime.now(UTC)))
        raise OSError("offline executor failed")

    case.answer = fail
    if command == "preread":
        with pytest.raises((OSError, order.PlanError)):
            order.preread(case.plan, case.root, case.config)
        count = 1
    else:
        result = order_perspectives.run(case.plan, case.root, case.config, case.store)
        count = len(order_perspectives.SEATS)
        assert len(result["failures"]) == count
    assert case.observations == [1] * count
    assert capacity.machine_runs(case.store, datetime.now(UTC)) == 0


def test_gate_leaves_a_slot_for_a_live_preread_then_starts_once(planning, git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path / "gate", monkeypatch, skill_libraries=[], test_command="true")
    case.store.finish_run(1, "error")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.record_label(1, "approved", "human")
    planner = StateStore(case.store.root.parent / "planner")
    planning.store = planner
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: planner))
    monkeypatch.setattr(capacity, "machine_root", lambda: planner.root.parent)
    capacity.write_state(planner, 1, case.clock[0])
    launched = []
    context = tick_context(case)
    context.launcher = lambda n: launched.append(n) or capacity.os.getpid()

    def answer(**kwargs):
        assert scheduler.tick(context).skipped is None
        assert launched == [], "Gate admitted work into the preread's machine slot"
        return ExecResult(0, False, "NONE", kwargs["log_path"])

    planning.answer = answer
    invoke(planning, "preread")
    assert scheduler.tick(context).skipped is None
    assert launched == [1]


def test_cancelled_wait_does_not_launch_or_cache_and_can_retry(planning, monkeypatch):
    case = planning
    sibling = StateStore(case.store.root.parent / "other")
    sibling.start_run(99, branch="other", worktree="other", pid=capacity.os.getpid())
    def interrupt(_):
        raise KeyboardInterrupt()
    monkeypatch.setattr(capacity.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        order.preread(case.plan, case.root, case.config)
    assert case.observations == []
    assert not (case.root / order.PREREAD_FILE).exists()
    assert capacity.machine_runs(case.store, datetime.now(UTC)) == 1
    sibling.finish_run(99, "merged")
    invoke(case, "preread")
    assert case.observations == [1]
    assert capacity.machine_runs(case.store, datetime.now(UTC)) == 0
