"""Boundaries of the machine-wide sleep ledger, the sensor history and runtime credit (#415)."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import capacity, run_waits, sleep_ledger
from foragent.state import StateStore

BASE = datetime(2026, 10, 20, 8, tzinfo=UTC)
NIGHT = 8 * 3600


class Facts:
    def __init__(self):
        self.clock = [BASE]
        self.intervals = []

    def awake(self, seconds):
        self.clock[0] += timedelta(seconds=seconds)

    def sleep(self, seconds):
        start = self.clock[0]
        self.clock[0] += timedelta(seconds=seconds)
        self.intervals.append((start, self.clock[0]))

    def slept_seconds(self, since):
        return sum(max(0.0, (end - max(start, since)).total_seconds()) for start, end in self.intervals)


def setup(tmp_path, monkeypatch, language="en", repo="repo"):
    facts = Facts()
    monkeypatch.setattr(capacity, "slept_seconds", facts.slept_seconds)
    store = StateStore(tmp_path / "data" / repo, now=lambda: facts.clock[0])
    ctx = SimpleNamespace(now=lambda: facts.clock[0], store=store, config=SimpleNamespace(language=language))
    return facts, store, ctx


def tick(ctx):
    report = SimpleNamespace(actions=[])
    return sleep_ledger.observe(ctx, report), report.actions


def test_sleep_after_an_ended_wait_is_credited_in_full(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    run = store.start_run(1, branch="b", worktree=tmp_path / "w")
    tick(ctx)
    store.update_run(1, waiting_until=(facts.clock[0] + timedelta(seconds=60)).isoformat(),
                     waited_seconds=60)
    facts.awake(60)
    facts.sleep(NIGHT)
    ledger, _ = tick(ctx)
    assert ledger.credit(run.started_at, facts.clock[0]) == NIGHT


def test_sibling_repository_runs_get_the_same_credit_and_keep_entries(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    sibling = StateStore(tmp_path / "data" / "other", now=lambda: facts.clock[0])
    run = sibling.start_run(7, branch="b", worktree=tmp_path / "w7")
    sibling.update_run(7, waiting_until=(BASE + timedelta(days=1)).isoformat())
    tick(ctx)  # This repository has no run; the sibling's run keeps the entry alive.
    facts.sleep(NIGHT)
    tick(ctx)
    ledger, _ = tick(ctx)
    assert ledger.credit(run.started_at, facts.clock[0]) == NIGHT


def test_test_slot_queue_time_excludes_sleep(tmp_path, monkeypatch):
    facts, store, _ = setup(tmp_path, monkeypatch)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    ctx = SimpleNamespace(now=lambda: facts.clock[0], store=store,
                          config=SimpleNamespace(test_slots=1, max_runtime_minutes=90))
    facts.awake(30 * 60)

    @contextmanager
    def slot(ceiling, waiting):
        facts.awake(60)
        waiting()
        facts.sleep(NIGHT)
        yield

    monkeypatch.setattr(capacity, "hold_test_slot", slot)
    with run_waits._test_slot(1, ctx):
        pass
    assert store.get_run(1).waited_seconds == 60
    assert run_waits._remaining(1, ctx) == 60 * 60, "30 min work leaves 60 min of a 90 min limit"


def test_wake_line_is_localized_and_once(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch, language="ko")
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    tick(ctx)
    facts.sleep(2 * 3600 + 5 * 60)
    _, actions = tick(ctx)
    assert len(actions) == 1 and "2시간 5분" in actions[0]
    assert tick(ctx)[1] == []


def test_ledger_prunes_without_running_runs_and_resets_when_the_clock_moves_back(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    tick(ctx)
    facts.sleep(NIGHT)
    ledger, actions = tick(ctx)
    assert actions and ledger.entries == []  # No running run needs the entry.
    path = capacity.machine_root() / sleep_ledger.LEDGER_FILE
    assert json.loads(path.read_text())["baseline_at"] == facts.clock[0].isoformat()
    facts.clock[0] = BASE - timedelta(hours=1)
    ledger, actions = tick(ctx)
    assert actions == [] and ledger.wake_at is None


def test_runner_remaining_runtime_subtracts_sleep(tmp_path, monkeypatch):
    facts, store, _ = setup(tmp_path, monkeypatch)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    ctx = SimpleNamespace(now=lambda: facts.clock[0], store=store,
                          config=SimpleNamespace(max_runtime_minutes=90))
    facts.awake(30 * 60)
    facts.sleep(NIGHT)
    assert run_waits._remaining(1, ctx) == 60 * 60


def fake_clocks(monkeypatch, platform, offset_counts_sleep=False):
    """Wall and monotonic clocks; Darwin also reports its last sleep pair.

    By default monotonic includes sleep, so only the kernel pair can find it.
    """
    state = SimpleNamespace(wall=BASE.timestamp(), awake=10.0, pair=None)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(state.wall, UTC)

    monkeypatch.setattr(capacity, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(capacity, "datetime", Clock)
    monkeypatch.setattr(capacity, "time", SimpleNamespace(time=lambda: state.wall, monotonic=lambda: state.awake))
    monkeypatch.setattr(capacity, "_darwin_sleep_interval", lambda: state.pair)

    def sleep(seconds):
        start = datetime.fromtimestamp(state.wall, UTC)
        state.wall += seconds
        if not offset_counts_sleep:
            state.awake += seconds
        state.pair = (start, datetime.fromtimestamp(state.wall, UTC))

    def awake(seconds):
        state.wall += seconds
        state.awake += seconds

    return sleep, awake


def test_darwin_keeps_every_sleep_not_only_the_kernels_last(tmp_path, monkeypatch):
    sleep, awake = fake_clocks(monkeypatch, "darwin")
    assert capacity.slept_seconds(BASE) == 0
    sleep(NIGHT)
    awake(60)
    assert capacity.slept_seconds(BASE) == NIGHT
    sleep(NIGHT)  # Read after each wake: the kernel pair names only the second sleep.
    awake(60)
    assert capacity.slept_seconds(BASE) == 2 * NIGHT


def test_darwin_counts_a_sleep_the_kernel_pair_no_longer_names(tmp_path, monkeypatch):
    sleep, awake = fake_clocks(monkeypatch, "darwin", offset_counts_sleep=True)
    assert capacity.slept_seconds(BASE) == 0
    sleep(NIGHT)
    awake(60)
    sleep(NIGHT)  # No read between the two sleeps.
    awake(60)
    assert capacity.slept_seconds(BASE) == 2 * NIGHT


def test_fallback_history_survives_many_newer_reads(tmp_path, monkeypatch):
    sleep, awake = fake_clocks(monkeypatch, "other", offset_counts_sleep=True)
    assert capacity.slept_seconds(BASE) == 0
    sleep(NIGHT)
    for _ in range(200):  # Many newer recorded points must not evict the run's history.
        awake(60)
        capacity.slept_seconds(datetime.fromtimestamp(capacity.time.time(), UTC))
    assert capacity.slept_seconds(BASE) == NIGHT
    assert capacity.slept_seconds(BASE) == NIGHT


def test_a_tick_reads_its_time_inside_the_ledger_lock(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    tick(ctx)
    facts.sleep(NIGHT)
    held = []
    lock = capacity._flock

    @contextmanager
    def waited(path):
        with lock(path):
            held.append(path.name)
            if path.name == f"{sleep_ledger.LEDGER_FILE}.lock":
                facts.awake(5)  # Another repository's tick held the lock meanwhile.
            yield
            held.pop()

    real_now = ctx.now
    ctx.now = lambda: (held and held[-1] == f"{sleep_ledger.LEDGER_FILE}.lock" and real_now()) or pytest_fail()
    monkeypatch.setattr(capacity, "_flock", waited)
    ledger, actions = tick(ctx)
    assert actions and ledger.grace_until == facts.clock[0] + sleep_ledger.WAKE_GRACE


def pytest_fail():
    raise AssertionError("observe read the time outside the ledger lock")


def test_an_older_clock_keeps_facts_dated_before_it(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    run = store.start_run(1, branch="b", worktree=tmp_path / "w")
    tick(ctx)
    facts.sleep(NIGHT)
    tick(ctx)
    facts.awake(600)
    tick(ctx)
    facts.clock[0] -= timedelta(seconds=300)  # Older than the stored baseline, after the sleep.
    ledger, _ = tick(ctx)
    assert ledger.credit(run.started_at, facts.clock[0]) == NIGHT
    assert ledger.wake_at is not None


def test_awake_time_after_wake_is_not_credited_as_sleep(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    tick(ctx)
    facts.sleep(NIGHT)
    facts.awake(1)
    progress = facts.clock[0].isoformat()
    facts.awake(4 * 60)  # The first tick after wake comes four minutes later.
    ledger, _ = tick(ctx)
    assert ledger.credit(progress, facts.clock[0]) == pytest.approx(0, abs=sleep_ledger.PLACEMENT_SECONDS)
    store.update_run(1, last_progress_at=progress)
    assert not ledger.defers_stall(store.get_run(1), facts.clock[0]), "Progress after wake ends grace"


def test_darwin_sysctl_recovery_does_not_record_a_sleep_twice(tmp_path, monkeypatch):
    sleep, awake = fake_clocks(monkeypatch, "darwin", offset_counts_sleep=True)
    pair = capacity._darwin_sleep_interval
    monkeypatch.setattr(capacity, "_darwin_sleep_interval", lambda: None)
    assert capacity.slept_seconds(BASE) == 0
    sleep(NIGHT)
    awake(60)
    assert capacity.slept_seconds(BASE) == NIGHT  # The clock offset alone found it.
    monkeypatch.setattr(capacity, "_darwin_sleep_interval", pair)
    awake(60)
    assert capacity.slept_seconds(BASE) == NIGHT


def test_two_sleeps_seen_together_place_each_sleep_conservatively(tmp_path, monkeypatch):
    sleep, awake = fake_clocks(monkeypatch, "linux-like", offset_counts_sleep=True)
    assert capacity.slept_seconds(BASE) == 0
    sleep(NIGHT)
    awake(600)
    second = datetime.fromtimestamp(capacity.time.time(), UTC)
    sleep(NIGHT)
    awake(60)
    assert capacity.slept_seconds(BASE) == 2 * NIGHT
    assert capacity.slept_seconds(second) <= NIGHT


def test_wake_grace_is_not_ended_by_activity_before_a_short_sleep(tmp_path, monkeypatch):
    # Baseline 08:00, progress 08:01:02, sleep 08:04-08:05:01; baseline + slept would be 08:01:01.
    facts, store, ctx = setup(tmp_path, monkeypatch)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    tick(ctx)
    facts.awake(62)
    store.update_run(1, last_progress_at=facts.clock[0].isoformat())
    facts.awake(178)
    facts.sleep(61)
    facts.awake(60)
    ledger, actions = tick(ctx)
    woke = BASE + timedelta(seconds=301)
    assert actions and woke <= ledger.wake_at <= woke + timedelta(seconds=sleep_ledger.PLACEMENT_SECONDS)
    assert ledger.defers_stall(store.get_run(1), facts.clock[0])


def test_offset_found_sleep_wakes_no_earlier_than_the_read_that_found_it(tmp_path, monkeypatch):
    sleep, awake = fake_clocks(monkeypatch, "other", offset_counts_sleep=True)
    now = lambda: datetime.fromtimestamp(capacity.time.time(), UTC)  # noqa: E731
    store = StateStore(tmp_path / "data" / "repo", now=now)
    ctx = SimpleNamespace(now=now, store=store, config=SimpleNamespace(language="en"))
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    tick(ctx)
    awake(62)
    store.update_run(1, last_progress_at=now().isoformat())
    awake(178)
    sleep(61)
    awake(60)
    ledger, actions = tick(ctx)
    assert actions and ledger.wake_at == now(), "Only the read time is measured for clock-offset sleep"
    assert ledger.defers_stall(store.get_run(1), now())


def test_a_run_started_inside_a_tick_interval_gets_its_whole_sleep(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    tick(ctx)
    facts.awake(4 * 60)
    run = store.start_run(1, branch="b", worktree=tmp_path / "w")
    facts.awake(60)
    facts.sleep(NIGHT)
    facts.awake(60)
    ledger, _ = tick(ctx)
    assert ledger.credit(run.started_at, facts.clock[0]) == pytest.approx(NIGHT, abs=sleep_ledger.PLACEMENT_SECONDS)
    progress = (BASE + timedelta(minutes=5, seconds=1)).isoformat()  # One second into the sleep.
    assert ledger.credit(progress, facts.clock[0]) == pytest.approx(NIGHT - 1, abs=sleep_ledger.PLACEMENT_SECONDS)


def test_a_suspend_between_the_kernel_pair_and_the_offset_reads_counts_once(tmp_path, monkeypatch):
    sleep, awake = fake_clocks(monkeypatch, "darwin", offset_counts_sleep=True)
    pair = capacity._darwin_sleep_interval
    armed = []

    def suspending():
        seen = pair()
        if armed:
            armed.clear()
            sleep(NIGHT)  # The machine sleeps right after the kernel pair was read.
            awake(1)
        return seen

    monkeypatch.setattr(capacity, "_darwin_sleep_interval", suspending)
    assert capacity.slept_seconds(BASE) == 0
    armed.append(True)
    assert capacity.slept_seconds(BASE) == NIGHT
    awake(60)
    assert capacity.slept_seconds(BASE) == NIGHT


def suspend_after_first_read(facts, monkeypatch):
    """The machine sleeps right after the next sensor reading returns."""
    armed = [True]

    def reading(since):
        value = facts.slept_seconds(since)
        if armed:
            armed.clear()
            facts.sleep(NIGHT)
        return value

    monkeypatch.setattr(capacity, "slept_seconds", reading)


def test_remaining_runtime_survives_a_suspend_between_sensor_reads(tmp_path, monkeypatch):
    facts, store, _ = setup(tmp_path, monkeypatch)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    ctx = SimpleNamespace(now=lambda: facts.clock[0], store=store,
                          config=SimpleNamespace(max_runtime_minutes=90))
    facts.awake(30 * 60)
    suspend_after_first_read(facts, monkeypatch)
    assert run_waits._remaining(1, ctx) == 60 * 60, "An 8 h sleep mid-read must not use the budget"


def test_test_slot_credit_survives_a_suspend_between_sensor_reads(tmp_path, monkeypatch):
    facts, store, _ = setup(tmp_path, monkeypatch)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    ctx = SimpleNamespace(now=lambda: facts.clock[0], store=store,
                          config=SimpleNamespace(test_slots=1, max_runtime_minutes=90))
    facts.awake(30 * 60)

    @contextmanager
    def slot(ceiling, waiting):
        facts.awake(60)
        suspend_after_first_read(facts, monkeypatch)
        waiting()
        yield

    monkeypatch.setattr(capacity, "hold_test_slot", slot)
    with run_waits._test_slot(1, ctx):
        pass
    assert store.get_run(1).waited_seconds == 60, "Sleep during the credit is not waited time"
    monkeypatch.setattr(capacity, "slept_seconds", facts.slept_seconds)
    assert run_waits._remaining(1, ctx) == 60 * 60, "The sleep is credited once"


def test_slept_until_never_exceeds_its_window_when_suspends_repeat(tmp_path, monkeypatch):
    facts = Facts()

    def reading(since):
        facts.sleep(NIGHT)  # Every reading is followed by another suspend.
        return facts.slept_seconds(since)

    monkeypatch.setattr(capacity, "slept_seconds", reading)
    slept, moment = capacity.slept_until(BASE, lambda: facts.clock[0])
    assert 0 <= slept <= (moment - BASE).total_seconds()


def test_a_short_exact_sleep_keeps_every_second_for_grace_and_credit(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    run = store.start_run(1, branch="b", worktree=tmp_path / "w")
    tick(ctx)
    facts.awake(240)
    facts.sleep(60.1)  # Just over the wake threshold; the tick comes right at wake.
    ledger, actions = tick(ctx)
    assert len(actions) == 1 and ledger.grace_until is not None, "60.1 s of sleep starts the wake grace"
    assert sum(item["seconds"] for item in ledger.entries) == pytest.approx(60.1, abs=1e-4)
    assert ledger.credit(run.started_at, facts.clock[0]) == pytest.approx(60.1, abs=1e-4)
    assert ledger.defers_stall(store.get_run(1), facts.clock[0])
