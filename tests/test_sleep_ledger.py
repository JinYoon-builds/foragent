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
        """Like the real sensor, sleep up to its own now; a clock moved back sees no later sleep."""
        now = self.clock[0]
        return sum(max(0.0, (min(end, now) - max(start, since)).total_seconds()) for start, end in self.intervals)


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
    sleep, awake, ctx = offset_sensor_run(tmp_path, monkeypatch)
    ctx.store.update_run(1, waiting_until=(ctx.now() + timedelta(seconds=60)).isoformat(), waited_seconds=60)
    awake(60)
    ctx.store.update_run(1, waiting_until=None)
    awake(60)
    sleep(NIGHT)
    awake(60)
    assert run_waits._remaining(1, ctx) == 90 * 60 - 120, "Only the 120 awake seconds outside the wait count"


def test_sibling_repository_runs_get_the_same_credit_and_keep_entries(tmp_path, monkeypatch):
    sleep, awake = fake_clocks(monkeypatch, "other", offset_counts_sleep=True)
    now = lambda: datetime.fromtimestamp(capacity.time.time(), UTC)  # noqa: E731
    sibling = StateStore(capacity.machine_root() / "other", now=now)
    run = sibling.start_run(7, branch="b", worktree=tmp_path / "w7")
    sleep(NIGHT)
    awake(31 * 24 * 3600)  # Past the retention; the sibling's running run keeps the history.
    capacity.slept_seconds(now())
    assert capacity.slept_seconds(datetime.fromisoformat(run.started_at)) == pytest.approx(NIGHT)


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
    sleep, awake = fake_clocks(monkeypatch, "other", offset_counts_sleep=True)
    now = lambda: datetime.fromtimestamp(capacity.time.time(), UTC)  # noqa: E731
    capacity.slept_seconds(now())
    sleep(NIGHT)
    awake(60)
    assert capacity.slept_seconds(BASE) == NIGHT
    awake(60)
    capacity.slept_seconds(now())  # A later reader; no running run needs the old sleep.
    history = json.loads((capacity.machine_root() / capacity.SLEEP_HISTORY_FILE).read_text())
    assert history["intervals"] == [], "Sleep before every running run is pruned at once"
    facts, store, ctx = setup(tmp_path, monkeypatch)
    tick(ctx)
    facts.sleep(NIGHT)
    ledger, actions = tick(ctx)
    assert actions and ledger.wake_at is not None
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


def running_run(tmp_path):
    """A running run on this machine: its start keeps later sleep from pruning."""
    now = datetime.fromtimestamp(capacity.time.time(), UTC)
    store = StateStore(capacity.machine_root() / "repo", now=lambda: now)
    return store.start_run(1, branch="b", worktree=tmp_path / "w")


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
    running_run(tmp_path)
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
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    tick(ctx)
    facts.sleep(NIGHT)
    tick(ctx)
    facts.awake(600)
    tick(ctx)
    facts.clock[0] -= timedelta(seconds=300)  # Older than the stored baseline, after the sleep.
    ledger, _ = tick(ctx)
    assert ledger.wake_at is not None and ledger.grace_until is not None


def test_awake_time_after_wake_is_not_credited_as_sleep(tmp_path, monkeypatch):
    sleep, awake, ctx = offset_sensor_run(tmp_path, monkeypatch)
    sleep(NIGHT)
    awake(1)
    progress = ctx.now()
    ctx.store.update_run(1, last_progress_at=progress.isoformat())  # A write samples the sensor.
    awake(4 * 60)  # The first tick after wake comes four minutes later.
    (quiet,), _ = capacity.slept_until_many([progress], ctx.now)
    assert quiet == pytest.approx(0, abs=1e-3)


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
    running_run(tmp_path)
    assert capacity.slept_seconds(BASE) == 0
    sleep(NIGHT)
    awake(600)
    second = datetime.fromtimestamp(capacity.time.time(), UTC)
    capacity.sleep_sample(second)  # Every query start is sampled when it is stamped.
    sleep(NIGHT)
    awake(60)
    assert capacity.slept_seconds(BASE) == 2 * NIGHT
    assert capacity.slept_seconds(second) == pytest.approx(NIGHT)


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
    assert actions and ledger.wake_at >= BASE + timedelta(seconds=301), "Never earlier than the real wake"
    assert ledger.defers_stall(store.get_run(1), facts.clock[0])


def test_offset_found_sleep_wakes_no_earlier_than_the_read_that_found_it(tmp_path, monkeypatch):
    sleep, awake = fake_clocks(monkeypatch, "other", offset_counts_sleep=True)
    now = lambda: datetime.fromtimestamp(capacity.time.time(), UTC)  # noqa: E731
    store = StateStore(capacity.machine_root() / "repo", now=now)
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
    sleep, awake = fake_clocks(monkeypatch, "other", offset_counts_sleep=True)
    now = lambda: datetime.fromtimestamp(capacity.time.time(), UTC)  # noqa: E731
    store = StateStore(capacity.machine_root() / "repo", now=now)
    ctx = SimpleNamespace(now=now, store=store, config=SimpleNamespace(language="en"))
    tick(ctx)
    awake(4 * 60)
    run = store.start_run(1, branch="b", worktree=tmp_path / "w")
    awake(60)
    sleep(NIGHT)
    awake(60)
    tick(ctx)
    (credit,), _ = capacity.slept_until_many([datetime.fromisoformat(run.started_at)], now)
    assert credit == pytest.approx(NIGHT)


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
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    tick(ctx)
    facts.awake(240)
    facts.sleep(60.1)  # Just over the wake threshold; the tick comes right at wake.
    ledger, actions = tick(ctx)
    assert len(actions) == 1 and ledger.grace_until is not None, "60.1 s of sleep starts the wake grace"
    assert ledger.defers_stall(store.get_run(1), facts.clock[0])


def test_a_suspend_between_capturing_now_and_the_probe_is_placed_in_the_window(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    store.update_run(1, last_progress_at=(BASE - timedelta(minutes=10)).isoformat())
    tick(ctx)
    facts.awake(60)
    armed = [True]

    def now_then_suspend():
        moment = facts.clock[0]
        if armed:
            armed.clear()
            facts.sleep(NIGHT)  # The machine sleeps right after the tick read its time.
        return moment

    ctx.now = now_then_suspend
    ledger, actions = tick(ctx)
    assert len(actions) == 1, "The wake line appears for a suspend inside the observation"
    assert ledger.at == facts.clock[0], "The window ends after the suspend"
    assert ledger.defers_stall(store.get_run(1), ledger.at), "An already-stale run gets the wake grace"


def offset_sensor_run(tmp_path, monkeypatch, sample=True):
    """A real clock-offset sensor (no kernel pairs) and a run whose start is a sensor sample."""
    sleep, awake = fake_clocks(monkeypatch, "other", offset_counts_sleep=True)
    now = lambda: datetime.fromtimestamp(capacity.time.time(), UTC)  # noqa: E731
    store = StateStore(capacity.machine_root() / "repo", now=now)  # Stores live under the data root.
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    if sample:
        capacity.slept_seconds(now())  # The sensor's last sample before the work below.
    ctx = SimpleNamespace(now=now, store=store, config=SimpleNamespace(test_slots=1, max_runtime_minutes=90, language="en"))
    return sleep, awake, ctx


def queued(awake, sleep, monkeypatch):
    @contextmanager
    def slot(ceiling, waiting):
        awake(60)
        sleep(30 * 60)  # Before any read inside the wait.
        waiting()
        yield

    monkeypatch.setattr(capacity, "hold_test_slot", slot)


def test_a_wait_after_an_old_offset_sample_credits_its_sleep_once(tmp_path, monkeypatch):
    sleep, awake, ctx = offset_sensor_run(tmp_path, monkeypatch)
    awake(30 * 60)  # Work with no sensor read: the last sample is 30 min before the wait.
    queued(awake, sleep, monkeypatch)
    with run_waits._test_slot(1, ctx):
        pass
    assert ctx.store.get_run(1).waited_seconds == pytest.approx(60, abs=1e-3)
    assert run_waits._remaining(1, ctx) == 60 * 60, "30 min work leaves 60 of 90 min"


def test_an_awake_wait_of_60_s_with_30_min_sleep_leaves_60_min_after_30_min_work(tmp_path, monkeypatch):
    sleep, awake, ctx = offset_sensor_run(tmp_path, monkeypatch, sample=False)  # Only start_run samples.
    awake(30 * 60)
    queued(awake, sleep, monkeypatch)
    with run_waits._test_slot(1, ctx):
        pass
    assert ctx.store.get_run(1).waited_seconds == pytest.approx(60, abs=1e-3), "The sleep is not waited time"
    assert run_waits._remaining(1, ctx) == 60 * 60, "Not 90: the sleep is credited exactly once"


def test_a_missing_ledger_keeps_the_sleep_of_runs_already_running(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    facts.awake(30 * 60)
    facts.sleep(NIGHT)  # Before the first tick ever: there is no ledger yet.
    ledger, actions = tick(ctx)
    assert len(actions) == 1 and "8 h 0 min" in actions[0], "The first tick still sees the run's sleep"
    assert ledger.defers_stall(store.get_run(1), facts.clock[0])


def test_progress_starts_a_sampled_window_on_a_clock_offset_sensor(tmp_path, monkeypatch):
    sleep, awake, ctx = offset_sensor_run(tmp_path, monkeypatch)
    tick(ctx)  # Baseline sample.
    awake(30 * 60)
    progress = ctx.now()
    ctx.store.update_run(1, last_progress_at=progress.isoformat())
    sleep(NIGHT)
    awake(301)
    (quiet,), _ = capacity.slept_until_many([progress], ctx.now)
    assert quiet == pytest.approx(NIGHT), "All 8 h after progress are sleep, not 7.5 h"


def test_a_run_started_at_hour_14_is_credited_only_the_later_suspend(tmp_path, monkeypatch):
    sleep, awake = fake_clocks(monkeypatch, "other", offset_counts_sleep=True)
    now = lambda: datetime.fromtimestamp(capacity.time.time(), UTC)  # noqa: E731
    store = StateStore(capacity.machine_root() / "repo", now=now)
    ctx = SimpleNamespace(now=now, store=store, config=SimpleNamespace(language="en"))
    tick(ctx)
    sleep(NIGHT)  # The first 8 h of a 16 h window asleep.
    awake(6 * 3600)
    run = store.start_run(1, branch="b", worktree=tmp_path / "w")  # Hour 14.
    sleep(2 * 3600)
    tick(ctx)
    (credit,), _ = capacity.slept_until_many([datetime.fromisoformat(run.started_at)], now)
    assert credit == pytest.approx(2 * 3600), "The run slept 2 h, not 4 h"



def test_sleep_before_the_oldest_running_run_is_pruned(tmp_path, monkeypatch):
    sleep, awake = fake_clocks(monkeypatch, "other", offset_counts_sleep=True)
    now = lambda: datetime.fromtimestamp(capacity.time.time(), UTC)  # noqa: E731
    store = StateStore(capacity.machine_root() / "repo", now=now)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    sleep(NIGHT)
    awake(60)
    store.finish_run(1, outcome="merged", detail="done")
    later = store.start_run(2, branch="b", worktree=tmp_path / "w2")  # Only a later run remains.
    awake(60)
    capacity.slept_seconds(datetime.fromisoformat(later.started_at))
    history = json.loads((capacity.machine_root() / capacity.SLEEP_HISTORY_FILE).read_text())
    assert history["intervals"] == [], "The old sleep precedes the oldest running run"


def test_a_run_from_before_the_change_keeps_its_wall_timed_wait_budget(tmp_path, monkeypatch):
    facts, store, _ = setup(tmp_path, monkeypatch)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    store.update_run(1, sleep_free_waits=False)  # Its old wall-timed test-slot wait held the sleep.
    ctx = SimpleNamespace(now=lambda: facts.clock[0], store=store,
                          config=SimpleNamespace(max_runtime_minutes=90))
    facts.awake(30 * 60)
    facts.sleep(NIGHT)
    store.update_run(1, waited_seconds=NIGHT)
    assert run_waits._remaining(1, ctx) == 60 * 60, "Not 9 h: the sleep is not credited twice"


def test_post_wake_activity_on_an_offset_sensor_ends_the_grace(tmp_path, monkeypatch):
    sleep, awake, ctx = offset_sensor_run(tmp_path, monkeypatch)
    ctx.store.update_run(1, last_progress_at=(ctx.now() - timedelta(minutes=10)).isoformat())
    tick(ctx)
    sleep(NIGHT)
    awake(5)
    ctx.store.update_run(1, last_progress_at=ctx.now().isoformat())  # Fresh progress after the wake.
    awake(241)
    ledger, actions = tick(ctx)
    assert actions, "The tick still reports the wake"
    assert not ledger.defers_stall(ctx.store.get_run(1), ctx.now()), "Newer progress ends the grace"
