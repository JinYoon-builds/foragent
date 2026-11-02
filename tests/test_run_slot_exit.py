"""Boundaries of returning-run admission (#1597): who owns a turn, the slot wait and its one exit record."""

from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace

import pytest

from foragent import capacity, orch_queue, run_merge_turn as turn, run_waits
from foragent.config import Config
from foragent.i18n import text
from foragent.run_attempt import ExecutorStopped
from foragent.state import StateStore
from foragent.tracker import FakeTracker

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = [NOW]
    store = StateStore(tmp_path / "machine" / "owner__project", now=lambda: clock[0])
    other = StateStore(tmp_path / "machine" / "other__project", now=lambda: clock[0])
    monkeypatch.setattr(capacity, "machine_root", lambda: store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    store.root.parent.mkdir(parents=True, exist_ok=True)
    (store.root.parent / "machine.yml").write_text("max_parallel: 1\ntest_slots: 1\n")
    capacity.write_state(store, 1, clock[0])
    tracker = FakeTracker()
    tracker.add_issue(1, "Work", "body", labels=["approved", "running"])
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1), pid=os.getpid())
    store.update_run(1, phase="ci")
    other.start_run(101, branch="b", worktree=other.worktree_path(101), pid=os.getpid())
    other.update_run(101, phase="implementing")
    sleeps = []
    ctx = SimpleNamespace(
        store=store, now=lambda: clock[0], clock=clock, config=Config(), tracker=tracker, board=None,
        repo="owner/project", monotonic=lambda: (clock[0] - NOW).total_seconds(),
        sleep=lambda seconds: None,
    )

    def sleep(seconds):
        sleeps.append((seconds, store.get_run(1).phase))
        clock[0] += timedelta(seconds=seconds)
        for hook in ctx.on_sleep:
            hook()

    ctx.on_sleep = []
    monkeypatch.setattr(capacity, "time", SimpleNamespace(
        sleep=sleep, monotonic=lambda: clock[0].timestamp(), time=lambda: clock[0].timestamp(),
    ))
    return SimpleNamespace(ctx=ctx, store=store, other=other, sleeps=sleeps, tracker=tracker)


def exits(case):
    return [row for row in case.store.flow_records() if row["kind"] == "run-slot-exit"]


def seat_in(case, n, target="main"):
    if case.store.get_run(n) is None:
        case.store.start_run(n, branch=f"b{n}", worktree=case.store.worktree_path(n), pid=os.getpid())
        case.store.update_run(n, phase="ci")
    place = turn.seat(case.ctx, n, target)
    turn.join(case.ctx, place, "head")
    return place


def test_holds_run_turn_reads_every_target_the_run_joined(case):
    assert not turn.holds_run_turn(case.store, 1), "Not in any queue"
    seat_in(case, 90, "main")
    own_main = seat_in(case, 1, "main")
    assert not turn.holds_run_turn(case.store, 1), "Behind a live run"
    seat_in(case, 1, "batch")
    assert turn.holds_run_turn(case.store, 1), "First on a second target, behind on main"
    turn.withdraw(case.ctx, turn.seat(case.ctx, 1, "batch"))
    assert not turn.holds_run_turn(case.store, 1), "Withdrawn from the target it held"
    case.store.finish_run(90, outcome="merged")
    assert turn.holds_run_turn(case.store, 1), "The live run ahead has ended"
    assert own_main.key == (1, case.store.get_run(1).started_at)


def test_holds_run_turn_ignores_another_execution_of_the_same_issue(case):
    seat_in(case, 1)
    case.ctx.clock[0] += timedelta(minutes=1)
    case.store.finish_run(1, outcome="crashed")
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1), pid=os.getpid())
    assert not turn.holds_run_turn(case.store, 1), "The earlier execution's seat is not this run's"


def test_holds_run_turn_follows_priority(case):
    seat_in(case, 90)
    seat_in(case, 1)
    orch_queue.set_global(case.store, [1, 90])
    assert turn.holds_run_turn(case.store, 1)
    assert not turn.holds_run_turn(case.store, 90)


def legacy_row(case, n, event="ready", **fields):
    turn._append(case.store, {"n": n, "event": event, **fields})


def started(case, n):
    return case.store.get_run(n).started_at


def test_a_row_that_names_no_target_holds_the_turn_like_its_named_rows(case):
    legacy_row(case, 1, started_at=started(case, 1))
    assert turn.holds_turn(case.store, turn.seat(case.ctx, 1, "main"))
    assert turn.holds_run_turn(case.store, 1)
    run_waits._set_phase(1, case.ctx, {"phase": "checking"})
    assert case.sleeps == [] and exits(case) == [] and case.store.get_run(1).phase == "checking"
    assert capacity.machine_runs(case.store, case.ctx.now()) == 2


def test_a_row_that_names_no_target_still_queues_and_withdraws(case):
    seat_in(case, 90)
    legacy_row(case, 90, started_at=started(case, 90))
    turn.withdraw(case.ctx, turn.seat(case.ctx, 90, "main"))
    legacy_row(case, 90, started_at=started(case, 90))
    legacy_row(case, 1, started_at=started(case, 1))
    assert not turn.holds_run_turn(case.store, 1), "Behind a live row that also names no target"
    legacy_row(case, 1, "withdrawn", started_at=started(case, 1))
    case.store.finish_run(90, outcome="merged")
    assert not turn.holds_run_turn(case.store, 1), "Withdrawn"


def test_a_row_of_another_run_that_names_no_target_keeps_a_named_seat_behind_it(case):
    seat_in(case, 90)
    legacy_row(case, 90, started_at=started(case, 90))
    seat_in(case, 1, "main")
    assert not turn.holds_turn(case.store, turn.seat(case.ctx, 1, "main"))
    assert not turn.holds_run_turn(case.store, 1)


def test_a_row_without_an_execution_follows_the_turn_reader(case):
    """Such a row is live yet is not this run's seat: both readers must give the same answer."""
    legacy_row(case, 1)
    assert not turn.holds_turn(case.store, turn.seat(case.ctx, 1, "main"))
    assert not turn.holds_run_turn(case.store, 1)


def test_finished_run_never_holds_a_turn(case):
    seat_in(case, 1)
    assert turn.holds_run_turn(case.store, 1)
    case.store.finish_run(1, outcome="merged")
    assert not turn.holds_run_turn(case.store, 1)
    assert not turn.holds_run_turn(case.store, 12345), "No run record"


def test_owner_is_admitted_above_the_budget_without_sleeping_and_counts_once(case):
    seat_in(case, 1)
    run_waits._set_phase(1, case.ctx, {"phase": "checking"})
    assert case.sleeps == [] and exits(case) == []
    assert case.store.get_run(1).phase == "checking"
    assert capacity.machine_runs(case.store, case.ctx.now()) == 2


def test_owner_does_not_need_the_machine_lock(case, monkeypatch):
    seat_in(case, 1)

    def locked(*args):
        pytest.fail("A turn owner must not queue behind the machine lock")

    monkeypatch.setattr(capacity, "_try_flock", locked)
    run_waits._set_phase(1, case.ctx, {"phase": "checking"})
    assert case.store.get_run(1).phase == "checking"


def test_one_try_restore_also_lets_an_owner_through(case):
    seat_in(case, 1)
    assert run_waits._set_phase(1, case.ctx, {"phase": "checking"}, resume=False)
    assert case.store.get_run(1).phase == "checking" and case.sleeps == []


def test_one_try_restore_leaves_a_nonholder_idle_and_records_no_exit(case):
    assert run_waits._set_phase(1, case.ctx, {"phase": "checking"}, resume=False) is False
    assert case.store.get_run(1).phase == "ci" and case.sleeps == []
    assert exits(case) == [] and "ready" not in case.tracker.get_issue(1).labels


def test_gaining_the_turn_during_the_wait_proceeds_at_the_next_poll(case):
    seat_in(case, 90)
    seat_in(case, 1)
    case.ctx.on_sleep.append(lambda: case.store.finish_run(90, outcome="merged"))
    run_waits._set_phase(1, case.ctx, {"phase": "checking"})
    assert [phase for _, phase in case.sleeps] == [capacity.RUN_SLOT_WAIT_PHASE]
    run = case.store.get_run(1)
    assert run.phase == "checking" and run.waiting_until is None and run.pause_regions == {}
    assert exits(case) == []


def test_slot_opening_admits_with_the_wait_credited_and_no_exit_record(case):
    case.ctx.on_sleep.append(lambda: case.other.finish_run(101, outcome="merged"))
    run_waits._set_phase(1, case.ctx, {"phase": "checking"})
    run = case.store.get_run(1)
    assert run.phase == "checking" and run.waiting_until is None and run.pause_regions == {}
    assert run.waited_seconds == capacity.WORKLOAD_POLL_SECONDS
    assert exits(case) == []
    assert case.tracker.get_issue(1).labels >= {"running"}, "Admitted work keeps running"


def test_queued_run_frees_its_slot_and_is_not_counted(case):
    observed = []
    case.ctx.on_sleep.append(lambda: observed.append(capacity.machine_runs(case.store, case.ctx.now())))
    with pytest.raises(ExecutorStopped):
        run_waits._set_phase(1, case.ctx, {"phase": "checking"})
    assert observed and set(observed) == {1}, "Only the other repository's worker holds the slot"


@pytest.mark.parametrize("minutes,limit", [(60, 600), (5, 300)], ids=["ten-minutes", "remaining-time"])
def test_exit_writes_exactly_one_countable_record(case, minutes, limit):
    case.ctx.config = Config(max_runtime_minutes=minutes)
    with pytest.raises(ExecutorStopped) as stopped:
        run_waits._set_phase(1, case.ctx, {"phase": "checking"})
    assert stopped.value.outcome == "limit-wait"
    run = case.store.get_run(1)
    rows = exits(case)
    assert len(rows) == 1
    assert rows[0]["issue"] == 1 and rows[0]["run_started_at"] == run.started_at
    assert rows[0]["data"] == {"waited_seconds": limit, "limit_seconds": limit}
    assert case.ctx.clock[0] - NOW == timedelta(seconds=limit), "The limit is measured at entry"
    assert run.phase == capacity.RUN_SLOT_WAIT_PHASE
    assert run.waiting_until is None and run.pause_regions == {} and run.waited_seconds == limit
    labels = case.tracker.get_issue(1).labels
    assert "ready" in labels and "running" not in labels and "blocked" not in labels


def test_each_exit_of_a_later_attempt_writes_its_own_record(case):
    for attempt in (1, 2):
        with pytest.raises(ExecutorStopped):
            run_waits._set_phase(1, case.ctx, {"phase": "checking"})
        assert len(exits(case)) == attempt
        case.store.finish_run(1, outcome="limit-wait")
        case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1), pid=os.getpid())
        case.store.update_run(1, phase="ci")
    first, second = exits(case)
    assert first["at"] < second["at"]


def test_unreadable_machine_state_is_not_reported_as_a_slot_exit(case, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(capacity, "_machine_scan", broken)
    with pytest.raises(OSError):
        run_waits._set_phase(1, case.ctx, {"phase": "checking"})
    assert exits(case) == [] and "ready" not in case.tracker.get_issue(1).labels


def test_slot_wait_is_an_idle_phase_with_visible_words():
    assert not capacity.phase_holds_slot(capacity.RUN_SLOT_WAIT_PHASE)
    for language in ("en", "ko"):
        assert text("lane_blocks_word_run-slot-wait", language)
        assert text("scorecard_label_phase_run-slot-wait", language)
