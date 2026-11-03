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
WHY = "Jin requested a stop"


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
    assert orch_stop.active(case.store) == "Jin requested a stop"
    requests = (case.store.root / "stop-requests.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in requests] == [
        {"at": NOW.isoformat(), "kind": "stop", "why": "Jin requested a stop", "held": ["api", "view", "main"]}]


def test_stop_all_is_repeatable_and_never_resumes_a_lane(case):
    earlier = NOW + timedelta(hours=1)
    orch_plan.pause(case.store, "api", earlier, "earlier human pause")
    orch.stop_all(case.ctx, "first")
    assert len(_events(case.store, "plan-pause")) == 3  # the earlier pause, then view and main once
    case.clock[0] = NOW + timedelta(minutes=5)
    orch.stop_all(case.ctx, "second")
    assert len(_events(case.store, "plan-pause")) == 3, "A held lane is not paused again"
    assert _events(case.store, "plan-resume") == []
    held = orch_plan.load(case.store, case.clock[0])["paused"]
    assert held["api"]["until"] == earlier.isoformat(), "A lane held before the stop keeps its own hold"
    assert all(held[lane]["until"] == orch_plan.UNTIL_RESUMED.isoformat() for lane in ("view", "main"))
    assert orch_stop.active(case.store) == "second"
    case.clock[0] = NOW + timedelta(days=2)  # The earlier pause has passed; the stop still holds every start.
    assert not orch_plan.lane_paused(case.store, "api", case.clock[0])
    assert orch_stop.active(case.store) == "second"


def test_resume_all_lifts_only_what_this_stop_held(case):
    orch_plan.pause(case.store, "api", NOW + timedelta(days=30), "a person held api first")
    orch.stop_all(case.ctx, "first")
    case.clock[0] = NOW + timedelta(minutes=1)
    orch.stop_all(case.ctx, "second")
    orch_plan.pause(case.store, "view", NOW + timedelta(days=30), "a person paused view during the stop")
    message = orch.resume_all(case.ctx, "Jin is back")
    assert "lanes resumed: main" in message
    assert orch_stop.active(case.store) is None
    assert orch_plan.lane_paused(case.store, "api", case.clock[0]), "Held before the stop: stays held"
    assert orch_plan.lane_paused(case.store, "view", case.clock[0]), "A person's own later pause stays"
    assert not orch_plan.lane_paused(case.store, "main", case.clock[0])
    assert len(_events(case.store, "plan-resume")) == 1, "Each held lane is resumed once"
    assert orch.resume_all(case.ctx) == "No project stop is in force; nothing was changed."
    assert len(_events(case.store, "plan-resume")) == 1
    last = json.loads((case.store.root / "stop-requests.jsonl").read_text().splitlines()[-1])
    assert (last["kind"], last["why"], last["resumed"]) == ("resume", "Jin is back", ["main"])
    orch.stop_all(case.ctx, "again")  # A new stop after the resume holds the free lane again.
    assert orch_stop.active(case.store) == "again"
    assert orch_plan.lane_paused(case.store, "main", case.clock[0])


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


def test_the_run_owner_stops_only_while_a_stop_is_in_force(case):
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.ctx.repo_root / "wt")
    orch_stop.raise_if_requested(case.ctx, 1)  # No stop yet.
    path = case.store.root / "stop-requests.jsonl"
    path.write_text(json.dumps({"at": NOW.isoformat(), "kind": "stop", "why": "old", "held": []}) + "\n"
                    + json.dumps({"at": NOW.isoformat(), "kind": "resume", "why": "", "resumed": []}) + "\n")
    orch_stop.raise_if_requested(case.ctx, 1)  # A stop that was resumed is not in force.
    with path.open("a") as stream:
        stream.write(json.dumps({"at": NOW.isoformat(), "kind": "stop", "why": WHY, "held": []}) + "\n{torn")
    with pytest.raises(ExecutorStopped) as stopped:
        orch_stop.raise_if_requested(case.ctx, 1)
    assert (stopped.value.outcome, str(stopped.value)) == ("stopped", WHY)
    case.store.finish_run(1, outcome="stopped", detail=WHY)
    orch_stop.raise_if_requested(case.ctx, 1)  # A finished run has nothing left to stop.
