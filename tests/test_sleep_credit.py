"""The gate tick alone credits machine sleep to run records (#415)."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import capacity, run_waits, sleep_credit
from foragent.state import StateStore

BASE = datetime(2026, 10, 20, 8, tzinfo=UTC)
NIGHT = 8 * 3600


class Facts:
    def __init__(self):
        self.clock = [BASE]
        self.intervals = []
        self.reads = []

    def now(self):
        return self.clock[0]

    def awake(self, seconds):
        self.clock[0] += timedelta(seconds=seconds)

    def sleep(self, seconds):
        start = self.clock[0]
        self.awake(seconds)
        self.intervals.append((start, self.clock[0]))

    def slept_seconds(self, since):
        self.reads.append(since)
        return sum(max(0.0, (end - max(start, since)).total_seconds()) for start, end in self.intervals)


@pytest.fixture
def case(tmp_path, monkeypatch):
    facts = Facts()
    monkeypatch.setattr(capacity, "slept_seconds", facts.slept_seconds)
    store = StateStore(tmp_path / "state", now=facts.now)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    return SimpleNamespace(facts=facts, store=store, tick=lambda: sleep_credit.credit(store, facts.now()))


def test_without_a_baseline_the_tick_credits_nothing_and_stores_one(case):
    case.facts.sleep(NIGHT)
    assert case.tick() == 0 and case.store.get_run(1).slept_seconds == 0
    baseline = json.loads((case.store.root / sleep_credit.BASELINE_FILE).read_text())
    assert baseline == {"at": case.facts.now().isoformat()}


def test_a_tick_credits_sleep_since_the_previous_tick(case):
    case.tick()
    case.facts.awake(30 * 60)
    case.facts.sleep(NIGHT)
    case.tick()
    run = case.store.get_run(1)
    assert run.slept_seconds == NIGHT and run.slept_since_progress == NIGHT
    case.tick()
    assert case.store.get_run(1).slept_seconds == NIGHT, "Reading does not consume or repeat credit"


@pytest.mark.parametrize("wait", ["test-slot-wait", "overlap-wait", "baseline-wait", "until"])
def test_a_waiting_run_gets_no_sleep_credit(case, wait):
    case.tick()
    if wait == "until":
        case.store.update_run(1, waiting_until=(case.facts.now() + timedelta(days=1)).isoformat())
    else:
        case.store.update_run(1, phase=wait)
    case.facts.sleep(NIGHT)
    case.tick()
    assert case.store.get_run(1).slept_seconds == 0, "Its wall-timed wait already covers the time"


def test_a_run_started_after_the_previous_tick_gets_nothing_from_the_next(case, tmp_path):
    case.tick()
    case.facts.awake(60)
    case.store.start_run(2, branch="b", worktree=tmp_path / "w2")
    case.facts.sleep(NIGHT)
    case.tick()
    assert case.store.get_run(2).slept_seconds == 0 and case.store.get_run(1).slept_seconds == NIGHT


def test_progress_after_the_previous_tick_resets_the_stall_credit_first(case):
    case.tick()
    case.facts.sleep(NIGHT)
    case.tick()
    case.facts.awake(60)
    case.store.update_run(1, last_progress_at=case.facts.now().isoformat())
    case.facts.sleep(600)
    case.tick()
    run = case.store.get_run(1)
    assert run.slept_since_progress == 600 and run.slept_seconds == NIGHT + 600


def test_the_increment_is_clamped_to_the_wall_time_since_the_previous_tick(case, monkeypatch):
    case.tick()
    case.facts.awake(60)
    monkeypatch.setattr(capacity, "slept_seconds", lambda since: 10 * NIGHT)
    assert case.tick() == 60
    monkeypatch.setattr(capacity, "slept_seconds", lambda since: -5.0)
    case.facts.awake(60)
    assert case.tick() == 0


def test_a_clock_moved_back_credits_nothing(case):
    case.tick()
    case.facts.sleep(NIGHT)
    case.facts.clock[0] = BASE - timedelta(hours=1)
    assert case.tick() == 0 and case.store.get_run(1).slept_seconds == 0


def test_remaining_runtime_reads_the_run_record_not_the_sensor(case, monkeypatch):
    case.tick()
    case.facts.awake(30 * 60)
    case.facts.sleep(NIGHT)
    case.tick()
    monkeypatch.setattr(capacity, "slept_seconds", lambda since: pytest.fail("The runner never reads the sensor"))
    ctx = SimpleNamespace(now=case.facts.now, store=case.store, config=SimpleNamespace(max_runtime_minutes=90))
    assert run_waits._remaining(1, ctx) == 60 * 60


def test_a_run_record_without_the_fields_reads_as_zero(case):
    path = case.store.root / "runs" / "1.json"
    record = json.loads(path.read_text())
    record.pop("slept_seconds"), record.pop("slept_since_progress")
    path.write_text(json.dumps(record))
    run = case.store.get_run(1)
    assert run.slept_seconds == 0 and run.slept_since_progress == 0


def test_sensor_clock_baselines_are_pruned_by_age_only(tmp_path, monkeypatch):
    wall, awake = [BASE.timestamp()], [10.0]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(wall[0], UTC)

    monkeypatch.setattr(capacity, "sys", SimpleNamespace(platform="other"))
    monkeypatch.setattr(capacity, "datetime", Clock)
    monkeypatch.setattr(capacity, "time", SimpleNamespace(time=lambda: wall[0], monotonic=lambda: awake[0]))
    capacity.slept_seconds(BASE)
    wall[0] += 47 * 3600
    capacity.slept_seconds(Clock.now())
    path = capacity.machine_root() / capacity.SLEEP_BASELINES_FILE
    assert len(json.loads(path.read_text())) == 2
    wall[0] += 2 * 3600
    capacity.slept_seconds(Clock.now())
    assert len(json.loads(path.read_text())) == 2, "Only the one older than 48 h is dropped"


def test_only_sleep_above_a_minute_starts_wake_grace(case):
    case.tick()
    case.facts.sleep(60)
    case.tick()
    assert sleep_credit.wake_grace(case.store) is None
    case.facts.sleep(61)
    case.tick()
    wake, until = sleep_credit.wake_grace(case.store)
    assert wake == case.facts.now() and until == wake + timedelta(seconds=sleep_credit.WAKE_GRACE_SECONDS)


def test_activity_ends_grace_only_for_its_run_and_a_short_sleep_does_not_restart_it(case, tmp_path):
    case.store.start_run(2, branch="b", worktree=tmp_path / "w2")
    case.tick()
    case.facts.sleep(NIGHT)
    case.tick()
    assert all(sleep_credit.in_wake_grace(case.store, case.store.get_run(n), case.facts.now()) for n in (1, 2))
    case.facts.awake(30)
    case.store.update_run(1, last_activity_at=case.facts.now().isoformat())
    case.facts.sleep(30)
    case.tick()
    assert not sleep_credit.in_wake_grace(case.store, case.store.get_run(1), case.facts.now())
    assert sleep_credit.in_wake_grace(case.store, case.store.get_run(2), case.facts.now())
    case.facts.awake(sleep_credit.WAKE_GRACE_SECONDS)
    assert not sleep_credit.in_wake_grace(case.store, case.store.get_run(2), case.facts.now())
