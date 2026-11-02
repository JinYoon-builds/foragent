from datetime import UTC, datetime, timedelta, timezone
import itertools
import json
import threading

import pytest
import yaml

from foragent import notify, orch_inbox, orch_queue, orch_tick_outcome, state_logs
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


def test_success_closes_its_alert_with_a_recorded_answer(store, pushes):
    config = Config(tick_failure_alert=1)
    record = orch_tick_outcome.record_failure(store, config, "owner/repo", RuntimeError("boom"))
    recovered = orch_tick_outcome.record_success(store)
    [request] = orch_inbox.requests(StateStore(store.root))
    assert request["id"] == record["alert_request"]
    assert request["status"] == "done"
    assert request["answer"] == f"gate tick recovered at {recovered['at']}"


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


def _flow_snapshot(store):
    return {path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in store.root.rglob("*") if path.is_file()}


def test_flow_records_read_creates_nothing_and_changes_nothing(store):
    root = store.root
    assert store.flow_records() == []
    assert not root.exists(), "Reading before any record must not create the state directory"
    store.record_flow("priority", issues=[1])
    store.record_flow("tick", issue=4, run_started_at="r", status="ok")
    before = _flow_snapshot(store)
    rows = store.flow_records()
    assert _flow_snapshot(store) == before
    assert [(r["kind"], r["issue"], r["run_started_at"], r["data"]) for r in rows] == [
        ("priority", None, None, {"issues": [1]}), ("tick", 4, "r", {"status": "ok"}),
    ]


def test_flow_window_is_half_open_and_time_uses_the_store_clock_in_utc(tmp_path):
    clock = [datetime(2026, 10, 24, 18, 0, tzinfo=timezone(timedelta(hours=9)))]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    for _ in range(3):
        store.record_flow("tick", status="ok")
        clock[0] += timedelta(seconds=1)
    start = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    assert store.flow_records()[0]["at"] == start.isoformat()
    window = store.flow_records(since=start + timedelta(seconds=1), until=start + timedelta(seconds=2))
    assert [row["at"] for row in window] == [(start + timedelta(seconds=1)).isoformat()]


def test_record_flow_inside_the_state_lock_does_not_take_it(store):
    done = threading.Event()

    def other_thread():
        store.record_flow("priority", issues=[1])
        done.set()

    with store._state_lock():
        thread = threading.Thread(target=other_thread, daemon=True)
        thread.start()
        # A writer that took the state lock would wait here; a wait in this thread would hang.
        assert done.wait(10), "A writer must not wait for the state lock"
        store.record_flow("priority", issues=[2])
    thread.join()
    assert [r["data"]["issues"] for r in store.flow_records()] == [[1], [2]]


def test_concurrent_writers_across_rotation_lose_and_reorder_nothing(tmp_path):
    ticks = itertools.count()
    start = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: start + timedelta(seconds=next(ticks)))

    def write(worker):
        for n in range(150):
            store.record_flow("priority", worker=worker, n=n)

    threads = [threading.Thread(target=write, args=(worker,)) for worker in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    rows = store.flow_records()
    assert len(rows) == 900
    assert [row["at"] for row in rows] == sorted(row["at"] for row in rows)
    assert len({row["at"] for row in rows}) == 900, "The clock is read once per record, inside the lock"
    for worker in range(6):
        assert [r["data"]["n"] for r in rows if r["data"]["worker"] == worker] == list(range(150))


def test_a_writer_arriving_during_the_clock_read_waits_so_file_order_is_time_order(tmp_path):
    start = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
    calls, second_done, threads = [], threading.Event(), []

    def clock():
        calls.append(None)
        at = start + timedelta(seconds=len(calls))
        if len(calls) == 1:
            def second():
                store.record_flow("priority", n=2)
                second_done.set()

            threads.append(threading.Thread(target=second))
            threads[0].start()
            assert not second_done.wait(0.2), "The first writer holds the lock while reading the clock"
        return at

    store = StateStore(tmp_path / "state", now=clock)
    store.record_flow("priority", n=1)
    threads[0].join()
    lines = (store.root / state_logs.FLOW_FILE).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["data"]["n"] for line in lines] == [1, 2]
    assert [row["data"]["n"] for row in store.flow_records()] == [1, 2]


def test_rotation_keeps_whole_generations_and_never_rewrites_accepted_lines(store):
    store_flow_lines = state_logs.FLOW_GENERATION_LINES
    for n in range(store_flow_lines):
        store.record_flow("priority", n=n)
    current = store.root / state_logs.FLOW_FILE
    first_generation = current.read_bytes()
    assert not (store.root / state_logs.FLOW_OLD_FILE).exists()
    store.record_flow("priority", n=store_flow_lines)
    assert (store.root / state_logs.FLOW_OLD_FILE).read_bytes() == first_generation
    assert [r["data"]["n"] for r in store.flow_records()] == list(range(store_flow_lines + 1))
    for n in range(store_flow_lines):
        store.record_flow("priority", n=n)
    rows = store.flow_records()
    assert len(rows) == store_flow_lines + 1, "The oldest generation is gone whole"
    assert rows[0]["data"]["n"] == store_flow_lines and rows[-1]["data"]["n"] == store_flow_lines - 1


def test_byte_bound_rotates_and_oversized_records_stay_readable_as_unknown(store):
    limit = state_logs.FLOW_RECORD_BYTES
    for n in range(10):
        store.record_flow("priority", n=n, blob="x" * (limit - 1000))
    total = sum(path.stat().st_size for path in store.root.iterdir())
    assert total <= 2 * state_logs.FLOW_GENERATION_BYTES <= 8 * 1024 * 1024
    assert (store.root / state_logs.FLOW_FILE).stat().st_size <= state_logs.FLOW_GENERATION_BYTES
    assert store.flow_records()[-1]["data"]["n"] == 9
    store.record_flow("priority", blob="x" * limit)
    newest = store.flow_records()[-1]
    assert list(newest["data"]) == ["oversized_bytes"] and newest["data"]["oversized_bytes"] > limit


def test_torn_or_malformed_lines_are_unknown_and_do_not_swallow_the_next_record(store):
    store.record_flow("priority", n=1)
    path = store.root / state_logs.FLOW_FILE
    with path.open("ab") as stream:
        stream.write(b'{"at": "2026-10-24T09:00:00+00:00", "kind": "tor')
    assert [r["data"]["n"] for r in store.flow_records()] == [1]
    store.record_flow("priority", n=2)
    with path.open("ab") as stream:
        stream.write(b'[1]\n{"at": "2026-10-24T09:00:00", "kind": "naive", "data": {}}\n'
                     b'{"at": "2026-10-24T09:00:00+00:00", "kind": "no-data"}\n\xff\n')
    store.record_flow("priority", n=3)
    assert [r["data"]["n"] for r in store.flow_records()] == [1, 2, 3]


def test_record_flow_failure_never_reaches_the_caller(store):
    store.root.parent.mkdir(parents=True, exist_ok=True)
    store.root.write_text("a file where the state directory belongs", encoding="utf-8")
    assert store.record_flow("priority", issues=[1]) is None
    assert store.flow_records() == []
    store.root.unlink()
    assert store.record_flow("priority", bad=object()) is None
    assert "object" in store.flow_records()[0]["data"]["bad"], "Unserializable values keep their text"
    cycle = []
    cycle.append(cycle)
    assert store.record_flow("priority", cycle=cycle) is None
    assert len(store.flow_records()) == 1


def test_a_failed_flow_write_keeps_tick_results_and_errors(store, pushes, monkeypatch):
    monkeypatch.setattr(store, "_append_flow_line", lambda line: 1 / 0)
    assert orch_tick_outcome.run_recorded(store, Config(), "o/r", lambda: "result") == "result"
    with pytest.raises(ValueError, match="original"):
        orch_tick_outcome.run_recorded(store, Config(), "o/r", lambda: (_ for _ in ()).throw(ValueError("original")))
    assert orch_tick_outcome.read(store)["status"] == "failed"


def test_tick_that_fails_to_acquire_the_lock_records_nothing(store):
    with store.tick_lock() as acquired:
        assert acquired
        result = []
        thread = threading.Thread(target=lambda: result.append(
            orch_tick_outcome.run_recorded(store, Config(), "o/r", lambda: "ran")))
        thread.start()
        thread.join()
    assert result == [None]
    assert store.flow_records() == []


def test_tick_flow_survives_a_failing_outcome_record_and_carries_steps(store, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("outcome disk")

    monkeypatch.setattr(orch_tick_outcome, "record_success", fail)

    def run():
        with orch_tick_outcome.step("fetch"):
            return "x"

    with pytest.raises(OSError, match="outcome disk"):
        orch_tick_outcome.run_recorded(store, Config(), "o/r", run)
    [row] = store.flow_records()
    assert row["kind"] == "tick" and row["data"]["status"] == "ok"
    assert [item["name"] for item in row["data"]["steps"]] == ["fetch"]


def test_priority_records_only_real_changes_and_tolerates_an_unreadable_list(store):
    orch_queue.set_global(store, [])
    assert store.flow_records() == [], "Clearing an already empty list changes nothing"
    path = orch_queue._global_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert orch_queue.set_global(store, [5, 5, 6]) == [5, 6]
    assert orch_queue.get_global(store) == [5, 6]
    orch_queue.set_global(store, [5, 6])
    assert [r["data"]["issues"] for r in store.flow_records()] == [[5, 6]]
    orch_queue.set_global(store, [])
    assert [r["data"]["issues"] for r in store.flow_records()] == [[5, 6], []]


def test_a_failed_priority_write_records_no_change(store, monkeypatch):
    def fail(path, value):
        raise OSError("disk")

    monkeypatch.setattr(store, "_write_json", fail)
    with pytest.raises(OSError, match="disk"):
        orch_queue.set_global(store, [1])
    assert store.flow_records() == []
