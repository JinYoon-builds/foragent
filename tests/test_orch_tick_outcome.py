from datetime import UTC, datetime, timedelta

import pytest
import yaml

from foragent import notify, orch_inbox, orch_tick_outcome
from foragent.config import Config, ConfigError, load_config
from foragent.state import StateStore


@pytest.fixture
def store(tmp_path):
    clock = [datetime(2026, 10, 24, 9, 0, tzinfo=UTC)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.clock = clock
    return store


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda config, title, line, link=None: sent.append(line) or True)
    return sent


def test_read_without_record_is_none(store):
    assert orch_tick_outcome.read(store) is None
    (store.root).mkdir(parents=True)
    orch_tick_outcome.outcome_path(store).write_text("not json", encoding="utf-8")
    assert orch_tick_outcome.read(store) is None


def test_long_message_is_cut_to_limit_and_log_keeps_it(store, pushes):
    try:
        raise ValueError("x" * 2000)
    except ValueError as exc:
        record = orch_tick_outcome.record_failure(store, Config(tick_failure_alert=5), "o/r", exc)
    assert len(record["error_message"]) == orch_tick_outcome.MESSAGE_LIMIT
    assert "x" * 2000 in (store.root / orch_tick_outcome.LOG_FILE).read_text(encoding="utf-8")
    assert pushes == []


def test_success_tolerates_a_removed_alert_request(store, pushes):
    config = Config(tick_failure_alert=1)
    record = orch_tick_outcome.record_failure(store, config, "o/r", RuntimeError("boom"))
    assert len(pushes) == 1
    (store.root / "inbox" / "requests" / f"{record['alert_request']}.json").unlink()
    store.clock[0] += timedelta(minutes=1)
    assert orch_tick_outcome.record_success(store)["consecutive_failures"] == 0
    assert orch_inbox.requests(store) == []


@pytest.mark.parametrize("value", [0, -1, "3", True])
def test_threshold_must_be_a_positive_integer(tmp_path, value):
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"tick_failure_alert": value}))
    with pytest.raises(ConfigError, match="tick_failure_alert"):
        load_config(tmp_path, global_path=tmp_path / "missing.yml")



def test_overlapping_tick_cannot_record_before_the_running_tick(store, pushes):
    import threading

    config = Config(tick_failure_alert=3)
    for n in range(2):
        with pytest.raises(ValueError):
            orch_tick_outcome.run_recorded(store, config, "o/r", lambda: (_ for _ in ()).throw(ValueError("x")))
    running, overlapping_done = threading.Event(), threading.Event()
    overlapping = []

    def failing_tick():
        running.set()
        assert overlapping_done.wait(5)  # The later tick finishes while this one still runs.
        raise ValueError("third failure")

    def later_tick():
        assert running.wait(5)
        other = StateStore(store.root, now=lambda: store.clock[0])
        overlapping.append(orch_tick_outcome.run_recorded(other, config, "o/r", lambda: "ok"))
        overlapping_done.set()

    thread = threading.Thread(target=later_tick)
    thread.start()
    with pytest.raises(ValueError, match="third failure"):
        orch_tick_outcome.run_recorded(store, config, "o/r", failing_tick)
    thread.join()
    assert overlapping == [None], "A tick that cannot take the lock runs nothing and records nothing"
    record = orch_tick_outcome.read(store)
    assert (record["status"], record["consecutive_failures"]) == ("failed", 3)
    assert len(orch_inbox.requests(store)) == 1 and len(pushes) == 1


def test_tick_inside_a_held_lock_reenters_but_plain_nesting_stays_busy(store):
    def inner():
        with store.tick_lock() as acquired:
            return acquired

    assert orch_tick_outcome.run_recorded(store, Config(), "o/r", inner) is True
    with store.tick_lock() as outer, store.tick_lock() as nested:
        assert (outer, nested) == (True, False)
    assert orch_tick_outcome.read(store)["status"] == "ok"


def test_step_outside_a_recorded_tick_only_runs_the_step(capsys):
    with orch_tick_outcome.step("alone"):
        pass
    assert orch_tick_outcome.timed("alone", lambda value: value + 1, 1) == 2
    assert capsys.readouterr().err == ""


def test_recorded_steps_keep_order_and_never_exceed_duration(store, pushes, monkeypatch, capsys):
    clock = [0.0]
    monkeypatch.setattr(orch_tick_outcome.time, "monotonic", lambda: clock[0])

    def tick():
        for name, seconds in (("first", 0.1), ("second", 0.2)):
            with orch_tick_outcome.step(name):
                clock[0] += seconds

    orch_tick_outcome.run_recorded(store, Config(), "o/r", tick)
    record = orch_tick_outcome.read(store)
    assert [item["name"] for item in record["steps"]] == ["first", "second"]
    assert sum(item["seconds"] for item in record["steps"]) <= record["duration_seconds"]
    assert capsys.readouterr().err == ""


def test_failing_step_records_its_elapsed_time_and_slow_line(store, pushes, monkeypatch, capsys):
    clock = [0.0]
    monkeypatch.setattr(orch_tick_outcome.time, "monotonic", lambda: clock[0])

    def tick():
        with orch_tick_outcome.step("slow"):
            clock[0] += 61.0
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        orch_tick_outcome.run_recorded(store, Config(), "o/r", tick)
    record = orch_tick_outcome.read(store)
    assert record["steps"] == [{"name": "slow", "seconds": 61.0}]
    assert capsys.readouterr().err.count("slow") == 1


def test_github_wait_budget_failure_records_its_step_and_slow_line(store, pushes, monkeypatch, capsys):
    from types import SimpleNamespace

    from foragent import scheduler

    clock = [0.0]
    monkeypatch.setattr(orch_tick_outcome.time, "monotonic", lambda: clock[0])
    store.github_wait_until = lambda now: now + timedelta(minutes=5)

    def budgets(ctx, ready):
        clock[0] += 75.0
        raise RuntimeError("budget failed")

    monkeypatch.setattr(scheduler, "_apply_budgets", budgets)
    ctx = SimpleNamespace(store=store, now=lambda: store.clock[0])
    with pytest.raises(RuntimeError):
        orch_tick_outcome.run_recorded(
            store, Config(), "o/r", lambda: scheduler._github_wait(ctx, {}, "github_wait_check"))
    record = orch_tick_outcome.read(store)
    assert record["steps"] == [{"name": "github_wait_check", "seconds": 75.0}]
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and "github_wait_check" in err[0] and "75" in err[0]
