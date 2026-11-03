"""Boundary tests for the project-wide `fa orch stop --all` (#1491).

Processes are a fake table only: no test sends a real signal.
"""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import orch, orch_plan, orch_stop, processes, scheduler, scheduler_start
from foragent.run_attempt import ExecutorStopped
from foragent.state import StateStore

NOW = datetime(2026, 10, 27, tzinfo=UTC)
RUNNER = 42000


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = [NOW]
    store = StateStore(tmp_path / "state", lambda: clock[0])
    monkeypatch.setattr(processes, "observe_tree", lambda identities=(), roots=(): list(identities))
    monkeypatch.setattr(scheduler, "TickContext", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(scheduler, "TickReport", lambda: SimpleNamespace(actions=[]))
    ctx = SimpleNamespace(repo="o/r", repo_root=tmp_path, tracker=None, store=store, git=None,
                          config=SimpleNamespace(language="en", lanes={"api": {}, "view": {}}),
                          now=lambda: clock[0], sleep=lambda s: None, monotonic=lambda: 0.0)
    return SimpleNamespace(ctx=ctx, store=store, clock=clock)


def _events(store, kind):
    path = store.root / "orch-events.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    return [event for event in map(json.loads, lines) if event["kind"] == kind]


def test_stop_all_holds_every_lane_including_main_through_the_plan(case):
    message = orch.stop_all(case.ctx, "  Jin   requested a stop ")
    for lane in ("api", "view", "main"):
        assert orch_plan.lane_paused(case.store, lane, NOW + timedelta(days=3650)), lane
        assert scheduler_start._lane_held(SimpleNamespace(store=case.store), lane, NOW), lane
    assert "api, view, main" in message and "Why: Jin requested a stop" in message
    requests = (case.store.root / "stop-requests.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in requests] == [{"at": NOW.isoformat(), "why": "Jin requested a stop"}]


def test_stop_all_is_repeatable_and_never_resumes_a_lane(case):
    orch_plan.pause(case.store, "api", NOW + timedelta(hours=1), "earlier human pause")
    orch.stop_all(case.ctx, "first")
    pauses = len(_events(case.store, "plan-pause"))
    assert pauses == 4  # the earlier one, then each of the three lanes once
    case.clock[0] = NOW + timedelta(minutes=5)
    orch.stop_all(case.ctx, "second")
    assert len(_events(case.store, "plan-pause")) == pauses, "A held lane is not paused again"
    assert _events(case.store, "plan-resume") == []
    held = orch_plan.load(case.store, case.clock[0])["paused"]
    assert set(held) == {"api", "view", "main"}
    assert all(item["until"] == orch_plan.UNTIL_RESUMED.isoformat() for item in held.values())
    assert len((case.store.root / "stop-requests.jsonl").read_text().splitlines()) == 2


def test_stop_all_signals_nothing_and_leaves_every_run_to_its_owner(case, monkeypatch):
    for n in (1, 2):
        case.store.start_run(n, branch=f"foragent/issue-{n}", worktree=case.ctx.repo_root / f"wt{n}")
        case.store.update_run(n, pid=RUNNER + n, process_identities=[])
    before = {n: case.store.get_run(n) for n in (1, 2)}
    monkeypatch.setattr(scheduler, "_kill_run", lambda *a, **k: pytest.fail("A project stop signals nothing"))
    monkeypatch.setattr(processes, "kill_process_group", lambda *a, **k: pytest.fail("A project stop signals nothing"))
    message = orch.stop_all(case.ctx, "Jin requested a stop")
    assert {n: case.store.get_run(n) for n in (1, 2)} == before, "Only each run's owner saves and finishes it"
    assert "#1: saves its unfinished work" in message and "#2: saves its unfinished work" in message


def test_the_hold_outlives_the_plan_expiry_until_a_person_resumes(case):
    orch_plan.set_plan(case.store, {"api": 2}, NOW + timedelta(hours=1))
    orch.stop_all(case.ctx, "Jin requested a stop")
    later = NOW + timedelta(days=2)
    case.clock[0] = later
    for lane in ("api", "view", "main"):
        assert orch_plan.lane_paused(case.store, lane, later), lane
    assert orch_plan.load(case.store, later)["width"] == {}, "The rest of the expired plan still lapses"
    orch_plan.pause(case.store, "view", later + timedelta(hours=1), "a shorter human pause")  # An edit keeps holds.
    assert orch_plan.lane_paused(case.store, "api", later + timedelta(days=1))
    assert orch_plan.resume(case.store, "api")
    assert not orch_plan.lane_paused(case.store, "api", later)
    assert orch_plan.lane_paused(case.store, "main", later)


def test_the_run_owner_stops_only_for_a_request_after_its_start(case):
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.ctx.repo_root / "wt")
    orch_stop.raise_if_requested(case.ctx, 1)  # No request yet.
    path = case.store.root / "stop-requests.jsonl"
    path.write_text(json.dumps({"at": (NOW - timedelta(seconds=1)).isoformat(), "why": "old"}) + "\n")
    orch_stop.raise_if_requested(case.ctx, 1)  # A request before this run started is not for it.
    with path.open("a") as stream:
        stream.write(json.dumps({"at": NOW.isoformat(), "why": "Jin requested a stop"}) + "\n{torn")
    with pytest.raises(ExecutorStopped) as stopped:
        orch_stop.raise_if_requested(case.ctx, 1)
    assert (stopped.value.outcome, str(stopped.value)) == ("stopped", "Jin requested a stop")
    case.store.finish_run(1, outcome="stopped", detail="Jin requested a stop")
    orch_stop.raise_if_requested(case.ctx, 1)  # A finished run has nothing left to stop.
