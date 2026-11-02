"""Why a ready issue did not start: each branch's cause, once per change, and nothing else moves."""

from dataclasses import replace
from datetime import timedelta

import pytest

from foragent import orch_plan, scheduler, scheduler_flow
from foragent.capacity import write_state
from foragent.state import StateStore
from foragent.tracker import TrackerError
from test_scheduler import BODY, NOW, approve, ctx  # noqa: F401


@pytest.fixture
def lane(ctx):  # noqa: F811
    ctx.config = replace(ctx.config, max_parallel=None, skill_libraries=[],
                         lanes={"orch": {"paths": ["src/foragent/**"]}})
    ctx.start_runs = True
    clock = [NOW]
    ctx.now = lambda: clock[0]
    ctx.starts = []

    def launch(number=None, *, kind="issue", lane=None):
        if kind == "issue":
            ctx.starts.append(number)
            return 7000 + number
        return None

    ctx.launcher = launch
    ctx.advance = lambda: clock.__setitem__(0, clock[0] + timedelta(seconds=1))
    write_state(ctx.store, 5, ctx.now())
    return ctx


def _add(context, number, files=None, *, running=False, labels=()):
    body = BODY + "\n## Files\n" + "".join(f"- {path}\n" for path in files or [f"src/f{number}.py"])
    approve(context, number, body=body, labels=["lane:orch", "running" if running else "ready", *labels])
    if running:
        context.store.start_run(number, branch=f"foragent/issue-{number}", lane="orch",
                                worktree=context.store.worktree_path(number), pid=7000 + number)


def _rows(context, number):
    return [row for row in context.store.flow_records() if row["kind"] == "ready" and row["issue"] == number]


def _reasons(context, number):
    return [row["data"]["reason"] for row in _rows(context, number)]


def _tick(context):
    report = scheduler.tick(context)
    context.advance()
    return report


def test_a_broken_loop_gives_every_later_candidate_the_slot_cause_without_their_checks(lane, monkeypatch):
    write_state(lane.store, 1, lane.now())
    _add(lane, 1, running=True)
    for number in (2, 3, 4):
        _add(lane, number)
    checked = []
    monkeypatch.setattr(scheduler, "_can_start_in_lane", lambda c, lane, issue: checked.append(issue.number) or True)
    assert _tick(lane).skipped is None
    assert lane.starts == [] and checked == []  # Naming the cause runs no check the break skipped.
    for number in (2, 3, 4):
        assert _reasons(lane, number) == ["slots-full"]
        assert _rows(lane, number)[0]["data"]["run_slots"] == {"limit": 1, "occupied": 1, "merge_waiters": 0}
        assert _rows(lane, number)[0]["data"]["outcome"] == "waiting"


def test_slots_filled_by_starts_hold_the_rest_and_the_started_row_names_its_run(lane):
    write_state(lane.store, 2, lane.now())
    for number in (1, 2, 3):
        _add(lane, number)
    _tick(lane)
    assert lane.starts == [1, 2]
    row = _rows(lane, 1)[-1]
    assert row["data"] == {"reason": None, "outcome": "started"}
    assert row["run_started_at"] == lane.store.get_run(1).started_at
    assert _reasons(lane, 3) == ["slots-full"]
    assert _rows(lane, 3)[0]["data"]["run_slots"]["occupied"] == 2
    _tick(lane)
    assert len(_rows(lane, 1)) == 1 and len(_rows(lane, 3)) == 1


def test_a_changed_decisive_fact_is_a_new_row(lane):
    write_state(lane.store, 1, lane.now())
    _add(lane, 1, running=True)
    _add(lane, 2, running=True)
    _add(lane, 3)
    _tick(lane)
    lane.store.update_run(1, phase="merge-lock-wait")  # An idle wait frees its machine slot.
    _tick(lane)
    assert lane.starts == []
    assert [row["data"]["run_slots"] for row in _rows(lane, 3)] == [
        {"limit": 1, "occupied": 2, "merge_waiters": 0}, {"limit": 1, "occupied": 1, "merge_waiters": 1},
    ]


def test_project_ceiling_scope_only_where_it_bound(lane):
    lane.config = replace(lane.config, max_parallel=1)
    _add(lane, 1, running=True)
    _add(lane, 2)
    _tick(lane)
    assert _rows(lane, 2)[0]["data"]["run_slots"] == {"limit": 1, "occupied": 1, "merge_waiters": 0}
    lane.config = replace(lane.config, max_parallel=4)
    write_state(lane.store, 1, lane.now())
    _tick(lane)
    assert lane.starts == []
    assert _rows(lane, 2)[-1]["data"]["run_slots"] == {"limit": 1, "occupied": 1, "merge_waiters": 0}
    assert lane.store.get_run(2) is None


def test_ceiling_figures_differ_from_the_machine_figures(lane, monkeypatch):
    lane.config = replace(lane.config, max_parallel=1)
    _add(lane, 1, running=True)
    _add(lane, 2)
    monkeypatch.setattr(scheduler, "machine_runs", lambda *a: 3)  # Other projects hold machine slots.
    monkeypatch.setattr(scheduler, "_machine_budget", lambda c, *a: setattr(c, "_tick_budget", 9) or 9)
    _tick(lane)
    assert _rows(lane, 2)[0]["data"]["run_slots"] == {"limit": 1, "occupied": 1, "merge_waiters": 0}
    monkeypatch.setattr(scheduler, "_machine_budget", lambda c, *a: setattr(c, "_tick_budget", 3) or 3)
    lane.store.update_run(1, phase="merge-turn-wait")
    _tick(lane)
    assert _rows(lane, 2)[-1]["data"]["run_slots"] == {"limit": 3, "occupied": 3, "merge_waiters": 1}


def _moves_after_each_decision(context, monkeypatch, counts):
    """Another gate's start or finish lands right after every slot decision of the start loop."""
    occupied = list(counts)
    monkeypatch.setattr(scheduler, "machine_runs", lambda *a: occupied[0])
    has_slot = scheduler._has_slot

    def decided(ctx, budget):
        answer = has_slot(ctx, budget)
        if len(occupied) > 1:
            occupied.pop(0)
        return answer
    monkeypatch.setattr(scheduler, "_has_slot", decided)
    monkeypatch.setattr(scheduler, "_machine_budget", lambda c, *a: setattr(c, "_tick_budget", 2) or 2)


def test_a_full_decision_keeps_its_figures_when_a_slot_frees_right_after(lane, monkeypatch):
    _add(lane, 1)
    _moves_after_each_decision(lane, monkeypatch, [2, 0])
    _tick(lane)
    assert lane.starts == []
    assert _rows(lane, 1)[0]["data"]["run_slots"]["occupied"] == 2
    assert _rows(lane, 1)[0]["data"]["run_slots"]["limit"] == 2


def test_a_free_slot_decision_keeps_its_figures_when_the_machine_fills_right_after(lane, monkeypatch):
    _add(lane, 1)
    orch_plan.set_plan(lane.store, {"orch": 0}, lane.now() + timedelta(hours=1))
    _moves_after_each_decision(lane, monkeypatch, [1, 5])
    _tick(lane)
    assert lane.starts == [] and _reasons(lane, 1) == ["free-slot"]
    assert _rows(lane, 1)[0]["data"]["run_slots"]["occupied"] == 1


def test_a_failed_reservation_keeps_the_figures_of_the_locked_recount(lane, monkeypatch):
    _add(lane, 1)
    _add(lane, 2)
    _moves_after_each_decision(lane, monkeypatch, [1, 3, 0])  # Loop: free. Locked recount: full. Then free again.
    _tick(lane)
    assert lane.starts == []
    assert {_reasons(lane, 1)[0], _reasons(lane, 2)[0]} == {"slots-full"}
    assert _rows(lane, 1)[0]["data"]["run_slots"]["occupied"] == 3


def test_the_check_after_a_start_keeps_its_figures(lane, monkeypatch):
    _add(lane, 1)
    _add(lane, 2)
    _moves_after_each_decision(lane, monkeypatch, [1, 1, 2, 0])  # Loop, locked recount, after the start, then free.
    _tick(lane)
    assert lane.starts == [1]
    assert _rows(lane, 2)[0]["data"]["run_slots"]["occupied"] == 2


def test_a_ceiling_decision_keeps_its_figures_when_a_run_ends_right_after(lane, monkeypatch):
    lane.config = replace(lane.config, max_parallel=1)
    _add(lane, 1, running=True)
    _add(lane, 2)
    monkeypatch.setattr(scheduler, "machine_runs", lambda *a: 1)
    monkeypatch.setattr(scheduler, "_machine_budget", lambda c, *a: setattr(c, "_tick_budget", 9) or 9)
    has_slot = scheduler._has_slot

    def decided(ctx, budget):
        answer = has_slot(ctx, budget)
        monkeypatch.setattr(lane.store, "active_runs", lambda: [])
        return answer
    monkeypatch.setattr(scheduler, "_has_slot", decided)
    _tick(lane)
    assert _rows(lane, 2)[0]["data"]["run_slots"] == {"limit": 1, "occupied": 1, "merge_waiters": 0}


def test_a_decision_without_figures_records_no_invented_ones(lane, monkeypatch):
    _add(lane, 1)
    monkeypatch.setattr(scheduler, "_has_slot", lambda ctx, budget: False)
    _tick(lane)
    assert _reasons(lane, 1) == ["slots-full"] and "run_slots" not in _rows(lane, 1)[0]["data"]


def test_a_full_cause_never_carries_the_figures_of_an_earlier_free_decision(lane, monkeypatch):
    _add(lane, 1)
    monkeypatch.setattr(scheduler, "_reserve_slot", lambda *a: False)
    _tick(lane)
    assert _reasons(lane, 1) == ["slots-full"] and "run_slots" not in _rows(lane, 1)[0]["data"]


@pytest.mark.parametrize("ceiling,budget,expected", [(None, 1, 1), (1, 9, 0)])
def test_merge_waiters_follow_the_scope_that_decided(lane, monkeypatch, ceiling, budget, expected):
    lane.config = replace(lane.config, max_parallel=ceiling)
    other = StateStore(lane.store.root.parent / "other", now=lane.now)
    other.start_run(9, branch="foragent/issue-9", worktree=other.worktree_path(9))
    other.update_run(9, phase="merge-lock-wait")
    monkeypatch.setattr(scheduler_flow.resource_sample, "machine_stores", lambda store: [other.root])
    write_state(lane.store, 1, lane.now())
    _add(lane, 1, running=True)
    _add(lane, 2)
    monkeypatch.setattr(scheduler, "machine_runs", lambda *a: 1)
    monkeypatch.setattr(scheduler, "_machine_budget", lambda c, *a: setattr(c, "_tick_budget", budget) or budget)
    _tick(lane)
    assert _rows(lane, 2)[0]["data"]["run_slots"]["merge_waiters"] == expected


def test_a_run_that_ends_during_launch_keeps_its_own_start_time(lane):
    _add(lane, 1)
    launch = lane.launcher

    def ends_at_once(number=None, **kwargs):
        pid = launch(number, **kwargs)
        lane.store.finish_run(number, outcome="merged")
        lane.advance()
        lane.store.start_run(number, branch="foragent/issue-1", lane="orch", worktree=lane.store.worktree_path(1))
        return pid
    lane.launcher = ends_at_once
    _tick(lane)
    assert _rows(lane, 1)[-1]["run_started_at"] == NOW.isoformat()


def test_ineligible_is_named_from_the_labels_that_decided_not_a_later_read(lane, monkeypatch):
    _add(lane, 1)
    lane.start_runs = False
    _tick(lane)  # Saves the approval, so the next tick reads the issue only in its start check.
    lane.start_runs = True
    get_issue = lane.tracker.get_issue
    monkeypatch.setattr(lane.tracker, "get_issue", lambda n: replace(get_issue(n), labels=get_issue(n).labels | {"blocked"}))
    _tick(lane)
    assert lane.starts == []
    assert _reasons(lane, 1)[-1] == "held-no-retry"
    assert _rows(lane, 1)[-1]["data"]["labels"] == ["blocked"]


def test_unreadable_merge_waiters_are_unknown_not_zero(lane, monkeypatch):
    write_state(lane.store, 1, lane.now())
    _add(lane, 1, running=True)
    _add(lane, 2)
    other = lane.store.root.parent / "other"
    (other / "runs").mkdir(parents=True)
    monkeypatch.setattr(scheduler_flow.resource_sample, "machine_stores", lambda store: [other])
    monkeypatch.setattr(StateStore, "active_runs", _failing_for(other, StateStore.active_runs))
    _tick(lane)
    assert _rows(lane, 2)[0]["data"]["run_slots"]["merge_waiters"] is None


def _failing_for(root, original):
    def active_runs(store):
        if store.root == root:
            raise OSError("unreadable")
        return original(store)
    return active_runs


def test_free_slot_only_when_the_machine_has_one_and_plan_width_holds(lane):
    write_state(lane.store, 3, lane.now())
    _add(lane, 1, running=True)
    _add(lane, 2)
    orch_plan.set_plan(lane.store, {"orch": 1}, lane.now() + timedelta(hours=1))
    _tick(lane)
    assert lane.starts == []
    assert _reasons(lane, 2) == ["free-slot"]
    assert _rows(lane, 2)[0]["data"]["run_slots"] == {"limit": 2, "occupied": 1, "merge_waiters": 0}


def test_plan_width_with_a_full_machine_is_slots_full_not_free_slot(lane):
    write_state(lane.store, 1, lane.now())
    _add(lane, 1, running=True)
    _add(lane, 2)
    orch_plan.set_plan(lane.store, {"orch": 1}, lane.now() + timedelta(hours=1))
    _tick(lane)
    assert _reasons(lane, 2) == ["slots-full"]


def test_file_overlap_then_start_clears_the_reason(lane):
    lane.config = replace(lane.config, overlap_per_file=1)
    _add(lane, 1, ["src/same.py"], running=True)
    _add(lane, 2, ["src/same.py"])
    _tick(lane)
    _tick(lane)
    assert lane.starts == [] and _reasons(lane, 2) == ["file-overlap"]
    assert "run_slots" not in _rows(lane, 2)[0]["data"]
    lane.store.finish_run(1, outcome="merged")
    lane.tracker.close_issue(1)
    lane.tracker.remove_labels(1, ["running", "ready"])
    _tick(lane)
    assert lane.starts == [2] and _reasons(lane, 2) == ["file-overlap", None]


def test_held_labels_are_held_no_retry_and_clearing_them_releases_once(lane):
    _add(lane, 1, labels=["blocked"])
    _tick(lane)
    _tick(lane)
    assert lane.starts == [] and _reasons(lane, 1) == ["held-no-retry"]
    assert _rows(lane, 1)[0]["data"]["labels"] == ["blocked"]
    lane.tracker.remove_labels(1, ["approved"])
    _tick(lane)
    _tick(lane)
    assert _reasons(lane, 1) == ["held-no-retry", "released"]
    assert _rows(lane, 1)[-1]["data"]["outcome"] == "waiting"


def test_closing_a_waiting_issue_releases_it_once(lane):
    write_state(lane.store, 1, lane.now())
    _add(lane, 1, running=True)
    _add(lane, 2)
    _tick(lane)
    lane.tracker.close_issue(2)
    lane.store.ingest([lane.tracker.get_issue(2)], lane.now(), {})
    _tick(lane)
    _tick(lane)
    assert _reasons(lane, 2) == ["slots-full", "released"]


def test_open_blockers_are_blocked_by_with_each_edge_from_the_read_already_made(lane, monkeypatch):
    _add(lane, 1)
    _add(lane, 2)
    _add(lane, 3, labels=[])
    lane.tracker.add_blocked_by(3, 1)
    lane.tracker.close_issue(2)
    lane.tracker.add_blocked_by(3, 2)
    reads = []
    blocked_by = lane.tracker.blocked_by
    monkeypatch.setattr(lane.tracker, "blocked_by", lambda n: (reads.append(n), blocked_by(n))[1])
    _tick(lane)
    assert 3 not in lane.starts
    assert reads.count(3) == 1, "The cause reuses the prerequisite read"
    assert lane.store.load_flag(3) is None
    assert _reasons(lane, 3) == ["blocked-by"]
    assert _rows(lane, 3)[0]["data"]["blocked_by"] == [
        {"number": 1, "state": "open", "repository": None},
        {"number": 2, "state": "closed", "repository": None},
    ]


def test_an_open_pr_is_not_reported_as_an_old_blocker(lane):
    _add(lane, 1)
    _add(lane, 2)
    lane.tracker.add_blocked_by(2, 1)
    _tick(lane)
    assert _reasons(lane, 2) == ["blocked-by"]
    lane.tracker.create_pr(head="foragent/issue-2", base="main", title="T", body="")
    lane.advance()
    _tick(lane)
    assert _reasons(lane, 2) == ["blocked-by", "released"]


def test_a_blocker_found_by_the_start_check_keeps_its_cause(lane, monkeypatch):
    _add(lane, 2)
    _add(lane, 1)
    prerequisites = scheduler._check_prerequisites
    monkeypatch.setattr(scheduler, "_check_prerequisites", _blocker_appears_after_consider(lane, prerequisites))
    _tick(lane)
    assert 2 not in lane.starts
    assert _reasons(lane, 2) == ["blocked-by"]
    assert _rows(lane, 2)[0]["data"]["blocked_by"] == [{"number": 1, "state": "open", "repository": None}]


def _blocker_appears_after_consider(context, prerequisites):
    calls = []

    def check(ctx, issue, approval, report, **kwargs):
        if issue.number == 2:
            calls.append(kwargs)
            if len(calls) == 2:  # The fresh read just before the start.
                context.tracker.add_blocked_by(2, 1)
        return prerequisites(ctx, issue, approval, report, **kwargs)
    return check


def test_a_flag_raised_by_a_start_check_is_held_no_retry(lane, monkeypatch):
    _add(lane, 1)
    prerequisites = scheduler._check_prerequisites
    calls = []

    def check(ctx, issue, approval, report, **kwargs):
        calls.append(issue.number)
        if len(calls) == 2:
            ctx.tracker.add_labels(1, ["blocked"])
            return False
        return prerequisites(ctx, issue, approval, report, **kwargs)
    monkeypatch.setattr(scheduler, "_check_prerequisites", check)
    _tick(lane)
    assert lane.starts == [] and _reasons(lane, 1) == ["held-no-retry"]
    assert _rows(lane, 1)[0]["data"]["labels"] == ["blocked"]


@pytest.mark.parametrize("start_runs,run_hours,reason", [
    (False, None, "starts-off"), (True, "__closed__", "outside-run-hours"),
])
def test_ticks_that_do_not_start_keep_their_own_reason(lane, start_runs, run_hours, reason, monkeypatch):
    lane.start_runs = start_runs
    if run_hours:
        monkeypatch.setattr(scheduler, "in_run_hours", lambda *a: False)
    _add(lane, 1)
    _tick(lane)
    assert lane.starts == [] and _reasons(lane, 1) == [reason]


def test_outbox_wait_names_every_candidate(lane):
    _add(lane, 1)
    _add(lane, 2)
    lane.tracker.flush_outbox = lambda: None  # Keep the workflow write undelivered through this tick.
    lane.store.queue_github_write(2, "add_labels", labels=["keep"])
    report = _tick(lane)
    assert lane.starts == [] and report.skipped is None
    assert (_reasons(lane, 1), _reasons(lane, 2)) == (["outbox-wait"], ["outbox-wait"])


def test_critical_memory_holds_all_candidates_with_that_cause(lane, monkeypatch):
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("critical", 0.0))
    _add(lane, 1)
    _add(lane, 2)
    _tick(lane)
    assert lane.starts == []
    assert (_reasons(lane, 1), _reasons(lane, 2)) == (["memory-held"], ["memory-held"])


def test_a_lane_hold_keeps_its_cause_when_demand_makes_the_slots_look_full(lane):
    _add(lane, 1, running=True)
    _add(lane, 2)
    orch_plan.pause(lane.store, "orch", lane.now() + timedelta(hours=1), "fixture")
    _tick(lane)  # Demand shrinks the tick's budget to the running run, so the loop breaks at once.
    assert lane.starts == [] and _reasons(lane, 2) == ["lane-held"]


def test_a_github_wait_names_stored_candidates_and_starts_nothing(lane):
    _add(lane, 1)
    _add(lane, 2, labels=["blocked"])
    lane.start_runs = False
    _tick(lane)  # The first observation of the stored issues.
    lane.start_runs = True
    lane.store.pause_github(lane.now() + timedelta(minutes=30))
    assert _tick(lane).skipped
    assert lane.starts == []
    assert _reasons(lane, 1)[-1] == "github-wait"
    assert _reasons(lane, 2) == ["held-no-retry"], "An early stop is no evidence that it left"


def test_a_limit_met_by_the_label_write_is_a_wait_not_a_start(lane, monkeypatch):
    _add(lane, 1)
    _add(lane, 2)
    def limited(*args, **kwargs):
        raise TrackerError("API rate limit exceeded")
    monkeypatch.setattr(lane.tracker, "add_labels", limited)
    monkeypatch.setattr(scheduler.orch_github, "pause", lambda c, exc: lane.now() + timedelta(minutes=5))
    report = _tick(lane)
    assert lane.starts == [] and report.skipped
    assert {_reasons(lane, 1)[-1], _reasons(lane, 2)[-1]} == {"github-wait"}
    assert all(row["data"]["outcome"] == "waiting" for row in _rows(lane, 1) + _rows(lane, 2))


def test_a_limit_met_inside_the_start_loop_names_the_rest(lane, monkeypatch):
    _add(lane, 1)
    def limited(*args, **kwargs):
        raise TrackerError("API rate limit exceeded")
    monkeypatch.setattr(scheduler, "_start_ready", limited)
    monkeypatch.setattr(scheduler.orch_github, "pause", lambda c, exc: lane.now() + timedelta(minutes=5))
    assert _tick(lane).skipped
    assert _reasons(lane, 1) == ["github-wait"]


def test_a_failed_tick_keeps_its_exception_and_invents_no_release(lane, monkeypatch):
    write_state(lane.store, 1, lane.now())
    _add(lane, 1, running=True)
    _add(lane, 2)
    _tick(lane)
    monkeypatch.setattr(scheduler, "_start_ready", _boom)
    with pytest.raises(RuntimeError, match="fixture"):
        scheduler.tick(lane)
    assert _reasons(lane, 2) == ["slots-full"]


def _boom(*args, **kwargs):
    raise RuntimeError("fixture")


def test_a_failed_refresh_invents_no_release(lane, monkeypatch):
    write_state(lane.store, 1, lane.now())
    _add(lane, 1, running=True)
    _add(lane, 2)
    _tick(lane)
    lane.tracker.remove_labels(2, ["ready"])
    lane.store.update_local_issue(2, labels=["lane:orch", "approved"])
    lane._refresh_failed = True
    with scheduler_flow.recording(lane, True):
        scheduler_flow.considered(lane, {})
    assert _reasons(lane, 2) == ["slots-full"]


@pytest.mark.parametrize("broken", ["flow_records", "record_flow"])
def test_recording_failure_leaves_the_tick_unchanged(lane, monkeypatch, broken):
    _add(lane, 1)
    def fail(*args, **kwargs):
        raise OSError("no disk")
    monkeypatch.setattr(lane.store, broken, fail)
    report = _tick(lane)
    assert lane.starts == [1] and report.skipped is None
    assert "#1 started running (pid=7001)" in report.actions
    assert lane.store.get_run(1).status == "running"


def test_a_busy_tick_records_nothing_and_a_direct_call_needs_no_tick(lane):
    _add(lane, 1)
    with lane.store.tick_lock() as acquired:
        assert acquired
        assert scheduler.tick(lane).skipped == scheduler.BUSY_TICK
    scheduler_flow.note(lane, 1, "file-overlap")
    assert lane.store.flow_records() == []


def test_recording_stays_local_and_does_not_read_the_tracker(lane, monkeypatch):
    write_state(lane.store, 1, lane.now())
    _add(lane, 1, running=True)
    _add(lane, 2)
    with scheduler_flow.recording(lane, True):
        monkeypatch.setattr(lane, "tracker", None)  # Any GitHub touch while naming causes would raise.
        scheduler_flow.slots_measured(lane, 1, 1, free=False, machine=True)
        scheduler_flow.hold_slots(lane, [2])
        scheduler_flow.hold_stored(lane, "github-wait")
        scheduler_flow.considered(lane, {"orch": [2]})
    assert _reasons(lane, 2) == ["slots-full"]
