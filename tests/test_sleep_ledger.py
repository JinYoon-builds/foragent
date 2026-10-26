"""Boundaries of the append-only sleep ledger and the credit each window takes from it (#415)."""

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
    """A wall clock and the real sleep intervals; the fake sensor reports sleep up to its now."""

    def __init__(self):
        self.clock = [BASE]
        self.intervals = []

    def now(self):
        return self.clock[0]

    def awake(self, seconds):
        self.clock[0] += timedelta(seconds=seconds)

    def sleep(self, seconds):
        start = self.clock[0]
        self.clock[0] += timedelta(seconds=seconds)
        self.intervals.append((start, self.clock[0]))

    def slept_seconds(self, since):
        now = self.clock[0]
        return sum(max(0.0, (min(end, now) - max(start, since)).total_seconds()) for start, end in self.intervals)


class OffsetFacts(Facts):
    """Like the Linux sensor: growth of a sleep total since its value at the first read at a point."""

    def __init__(self):
        super().__init__()
        self.baselines = {}

    def slept_seconds(self, since):
        total = sum((end - start).total_seconds() for start, end in self.intervals)
        return max(0.0, total - self.baselines.setdefault(since, total))


def setup(tmp_path, monkeypatch, repo="repo"):
    facts = Facts()
    monkeypatch.setattr(capacity, "slept_seconds", facts.slept_seconds)
    store = StateStore(capacity.machine_root() / repo, now=facts.now)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    ctx = SimpleNamespace(now=facts.now, store=store,
                          config=SimpleNamespace(test_slots=1, max_runtime_minutes=90, language="en"))
    return facts, store, ctx


def read(facts, starts=(), report=None, language="en", tick=False):
    return sleep_ledger.read(facts.now, starts, tick=tick, report=report, language=language)


def tick(facts, report=None, language="en"):
    return read(facts, report=report, language=language, tick=True)


def progress(store, facts):
    """Progress the way the runner writes it: stamped with a ledger reading's moment."""
    store.update_run(1, last_progress_at=sleep_ledger.mark(facts.now).isoformat())


def at(store, field="last_progress_at"):
    return datetime.fromisoformat(getattr(store.get_run(1), field))


def test_runner_remaining_runtime_subtracts_sleep(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    facts.awake(30 * 60)
    facts.sleep(NIGHT)
    assert run_waits._remaining(1, ctx) == 60 * 60, "30 min work plus 8 h asleep leaves 60 of 90 min"


def test_sleep_after_an_ended_wait_is_credited_in_full(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    store.update_run(1, waited_seconds=60)  # A monotonic-timed wait: no sleep inside it.
    facts.awake(120)
    facts.sleep(NIGHT)
    facts.awake(60)
    assert run_waits._remaining(1, ctx) == 90 * 60 - 120


def test_test_slot_queue_time_excludes_sleep(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    facts.awake(30 * 60)

    @contextmanager
    def slot(ceiling, waiting):
        facts.awake(60)
        facts.sleep(30 * 60)  # Before any reading inside the wait.
        waiting()
        yield

    monkeypatch.setattr(capacity, "hold_test_slot", slot)
    with run_waits._test_slot(1, ctx):
        pass
    assert store.get_run(1).waited_seconds == pytest.approx(60)
    assert run_waits._remaining(1, ctx) == 60 * 60, "The sleep is credited once, not as wait and sleep"


def test_wake_line_is_localized_and_once(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    tick(facts)
    facts.sleep(2 * 3600 + 5 * 60)
    report = []
    tick(facts, report, "ko")
    assert len(report) == 1 and "2시간 5분" in report[0]
    report = []
    tick(facts, report, "ko")
    assert report == []


def test_wake_grace_is_not_ended_by_activity_before_a_short_sleep(tmp_path, monkeypatch):
    # Reading 08:00, progress 08:01:02, sleep 08:04-08:05:01, first tick after wake 08:06:01.
    facts, store, ctx = setup(tmp_path, monkeypatch)
    tick(facts)
    facts.awake(62)
    progress(store, facts)
    facts.awake(178)
    facts.sleep(61)
    facts.awake(60)
    snapshot = tick(facts)
    assert snapshot.defers_stall([at(store)])
    assert snapshot.credit(at(store)) == pytest.approx(61)


def test_post_wake_activity_ends_the_grace(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    tick(facts)
    facts.sleep(NIGHT)
    facts.awake(5)
    progress(store, facts)  # Fresh progress after the wake.
    facts.awake(2)
    read(facts)  # A later reading, two seconds on, changes nothing.
    facts.awake(241)
    snapshot = tick(facts)
    assert not snapshot.defers_stall([at(store)]), "Newer progress ends the grace"
    assert snapshot.credit(at(store)) == 0


def test_a_concurrent_reader_between_progress_write_and_sample(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    facts.sleep(NIGHT)
    facts.awake(5)
    read(facts)  # Another reader records the sleep first.
    facts.awake(0.5)
    progress(store, facts)  # The progress is itself a reading: it finds nothing new.
    facts.awake(241)
    snapshot = tick(facts)
    assert snapshot.credit(datetime.fromisoformat(store.get_run(1).started_at)) == pytest.approx(NIGHT)
    assert snapshot.credit(at(store)) == 0, "The sleep ended before the progress"
    assert not snapshot.defers_stall([at(store)]), "The progress came after the wake"
    lines = (capacity.machine_root() / sleep_ledger.LEDGER_FILE).read_text().splitlines()
    assert len(lines) == 1, "One sleep, one record"


def test_two_sleeps_around_an_event_credit_only_the_later_one(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    facts.sleep(NIGHT)
    facts.awake(600)
    progress(store, facts)
    facts.sleep(2 * 3600)
    facts.awake(60)
    snapshot = read(facts)
    assert snapshot.credit(at(store)) == pytest.approx(2 * 3600)
    assert snapshot.credit(datetime.fromisoformat(store.get_run(1).started_at)) == pytest.approx(NIGHT + 2 * 3600)


def test_a_reader_with_no_active_runs_never_prunes_credit_another_run_needs(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch, repo="busy")
    facts.sleep(NIGHT)
    facts.awake(60)
    lone = StateStore(capacity.machine_root() / "idle", now=facts.now)
    assert lone.active_runs() == []
    for _ in range(3):  # Readers in a repository with nothing running.
        facts.awake(300)
        read(facts)
    assert read(facts).credit(datetime.fromisoformat(store.get_run(1).started_at)) == pytest.approx(NIGHT)


def test_only_history_before_the_oldest_running_run_is_pruned(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    facts.sleep(NIGHT)
    read(facts)
    store.finish_run(1, outcome="merged", detail="done")
    later = store.start_run(2, branch="b", worktree=tmp_path / "w2")  # Only a later run remains.
    facts.awake(60)
    assert read(facts).records == (), "The old sleep ended before the oldest running run started"
    facts.sleep(60)
    assert len(read(facts).records) == 1
    assert later.started_at


def test_an_8_h_sleep_survives_a_later_8_day_sleep_for_a_running_run(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    facts.awake(10 * 60)
    facts.sleep(NIGHT)
    read(facts)
    facts.awake(10 * 60)
    facts.sleep(8 * 24 * 3600)
    facts.awake(10 * 60)
    started = datetime.fromisoformat(store.get_run(1).started_at)
    assert read(facts).credit(started) == pytest.approx(NIGHT + 8 * 24 * 3600)
    assert run_waits._remaining(1, ctx) == 60 * 60, "30 min of work, however long the sleeps"


def test_a_clock_moved_back_ignores_intervals_after_it(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    facts.sleep(NIGHT)
    read(facts)
    facts.clock[0] = BASE - timedelta(hours=1)
    snapshot = tick(facts)
    assert snapshot.records == () and not snapshot.defers_stall([BASE - timedelta(hours=2)])


def test_a_suspend_between_the_two_sensor_reads_is_recorded_once(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    real = facts.slept_seconds
    armed = [True]

    def suspending(since):
        value = real(since)
        if armed:
            armed.clear()
            facts.sleep(NIGHT)  # Right after the reading's first sensor read.
        return value

    monkeypatch.setattr(capacity, "slept_seconds", suspending)
    snapshot = read(facts)
    started = datetime.fromisoformat(store.get_run(1).started_at)
    assert snapshot.now == facts.now() and snapshot.credit(started) == pytest.approx(NIGHT)
    assert snapshot.credit(started) == read(facts).credit(started)


def test_fallback_sensor_keeps_the_baseline_across_reads(tmp_path, monkeypatch):
    wall, awake = [1000.0], [10.0]
    monkeypatch.setattr(capacity, "sys", SimpleNamespace(platform="other"))
    monkeypatch.setattr(capacity, "time", SimpleNamespace(time=lambda: wall[0], monotonic=lambda: awake[0]))
    assert capacity.slept_seconds(BASE) == 0
    wall[0] += 500
    assert capacity.slept_seconds(BASE) == 500
    assert capacity.slept_seconds(BASE) == 500
    assert capacity.slept_seconds(BASE + timedelta(hours=1)) == 0
    history = json.loads((capacity.machine_root() / capacity.SLEEP_BASELINES_FILE).read_text())
    assert len(history) == 2



def test_a_ledger_created_after_the_sleep_still_credits_it(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    for name in (sleep_ledger.LEDGER_FILE, sleep_ledger.STATE_FILE):
        (capacity.machine_root() / name).unlink(missing_ok=True)  # An existing run; no ledger yet.
    facts.awake(30 * 60)
    facts.sleep(NIGHT)
    assert run_waits._remaining(1, ctx) == 60 * 60, "The sensor reports the sleep; it is credited"


def test_a_second_sleep_between_a_progress_write_and_its_reading(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    tick(facts)
    facts.sleep(NIGHT)
    progress(store, facts)  # The stamp is a reading: no sleep can fall between it and the record.
    facts.sleep(2 * 3600)
    facts.awake(10)
    snapshot = tick(facts)
    assert snapshot.credit(at(store)) == pytest.approx(2 * 3600), "The 2 h after progress count"
    assert snapshot.defers_stall([at(store)]), "Sleep followed the progress: the grace holds"


def test_two_45_s_sleeps_between_ticks_open_the_grace(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    tick(facts)
    facts.sleep(45)
    read(facts)  # A runner's reading.
    facts.awake(30)
    facts.sleep(45)
    read(facts)
    facts.awake(30)
    report = []
    snapshot = tick(facts, report)
    assert len(report) == 1 and snapshot.defers_stall([BASE])


def test_the_grace_starts_at_the_tick_that_sees_the_wake(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    tick(facts)
    facts.sleep(NIGHT)
    read(facts)  # Another run's reading records the wake first.
    facts.awake(301)
    snapshot = tick(facts)
    assert snapshot.grace_until == facts.now() + sleep_ledger.WAKE_GRACE
    assert snapshot.defers_stall([BASE]), "A silent run is not judged at the first tick after the wake"


def test_a_suspend_right_after_a_run_start_charges_no_pre_start_time(tmp_path, monkeypatch):
    facts = OffsetFacts()
    monkeypatch.setattr(capacity, "slept_seconds", facts.slept_seconds)
    store = StateStore(capacity.machine_root() / "repo", now=facts.now)
    other = StateStore(capacity.machine_root() / "other", now=facts.now)
    other.start_run(9, branch="b", worktree=tmp_path / "w9")  # A reading 101 min before the start.
    facts.awake(101 * 60)
    write = store._write_run
    armed = [True]

    def write_then_suspend(run):
        write(run)
        if armed:
            armed.clear()
            facts.sleep(NIGHT)  # Between the start's write and anything after it.

    monkeypatch.setattr(store, "_write_run", write_then_suspend)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    facts.awake(60)
    ctx = SimpleNamespace(now=facts.now, store=store, config=SimpleNamespace(max_runtime_minutes=90))
    assert run_waits._remaining(1, ctx) == 89 * 60, "Only the one awake minute is run time"


def test_a_lower_latest_only_sensor_reading_is_retried(tmp_path, monkeypatch):
    state = SimpleNamespace(wall=BASE.timestamp(), asleep=0.0, pair=None, armed=False)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(state.wall, UTC)

    def sleep(seconds):
        start = datetime.fromtimestamp(state.wall, UTC)
        state.wall += seconds
        state.asleep += seconds
        state.pair = (start, datetime.fromtimestamp(state.wall, UTC))

    def pair():
        seen = state.pair
        if state.armed:
            state.armed = False
            sleep(2 * 3600)  # A shorter suspend between the reading's two sensor reads.
        return seen

    monkeypatch.setattr(capacity, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(capacity, "datetime", Clock)
    monkeypatch.setattr(capacity, "_darwin_sleep_interval", pair)
    monkeypatch.setattr(capacity, "_darwin_asleep_total", lambda: state.asleep)
    now = Clock.now
    store = StateStore(capacity.machine_root() / "repo", now=now)
    run = store.start_run(1, branch="b", worktree=tmp_path / "w")
    sleep(NIGHT)
    state.wall += 60
    state.armed = True
    snapshot = sleep_ledger.read(now)
    assert snapshot.credit(datetime.fromisoformat(run.started_at)) == pytest.approx(NIGHT + 2 * 3600)


def test_darwin_sleep_since_boot_keeps_an_earlier_sleep_the_pair_forgets(tmp_path, monkeypatch):
    state = SimpleNamespace(wall=BASE.timestamp(), asleep=0.0, pair=None)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(state.wall, UTC)

    monkeypatch.setattr(capacity, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(capacity, "datetime", Clock)
    monkeypatch.setattr(capacity, "_darwin_sleep_interval", lambda: state.pair)
    monkeypatch.setattr(capacity, "_darwin_asleep_total", lambda: state.asleep)

    def sleep(seconds):
        start = datetime.fromtimestamp(state.wall, UTC)
        state.wall += seconds
        state.asleep += seconds
        state.pair = (start, datetime.fromtimestamp(state.wall, UTC))

    assert capacity.slept_seconds(BASE) == 0
    sleep(NIGHT)
    state.wall += 60
    sleep(2 * 3600)  # The pair now names only this one.
    assert capacity.slept_seconds(BASE) == NIGHT + 2 * 3600


def test_a_lost_checkpoint_write_does_not_record_the_sleep_twice(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    facts.sleep(NIGHT)
    replace = sleep_ledger._replace

    def fail_state(path, content):
        if path.name == sleep_ledger.STATE_FILE:
            raise OSError("interrupted")
        replace(path, content)

    monkeypatch.setattr(sleep_ledger, "_replace", fail_state)
    with pytest.raises(OSError):
        read(facts)  # The record is appended; the state write fails.
    monkeypatch.setattr(sleep_ledger, "_replace", replace)
    facts.awake(60)
    started = datetime.fromisoformat(store.get_run(1).started_at)
    assert read(facts).credit(started) == pytest.approx(NIGHT), "8 h, not 16 h"


def test_a_torn_last_line_does_not_swallow_the_next_record(tmp_path, monkeypatch):
    facts, store, ctx = setup(tmp_path, monkeypatch)
    with (capacity.machine_root() / sleep_ledger.LEDGER_FILE).open("a") as stream:
        stream.write('{"since": "2026-10-20T0')  # A crash mid-append.
    facts.sleep(NIGHT)
    started = datetime.fromisoformat(store.get_run(1).started_at)
    read(facts)
    assert read(facts).credit(started) == pytest.approx(NIGHT)
