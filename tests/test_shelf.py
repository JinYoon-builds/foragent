"""Shelf boundaries: exact limits, durable single effects, clocks and writer ownership."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import os

import pytest

from foragent import orch_events, orch_inbox, shelf, shelf_io
from foragent.processes import run_logged
from foragent.state import StateStore
from foragent.tracker import Issue


@pytest.fixture
def case(tmp_path):
    clock = [datetime(2031, 1, 1, tzinfo=UTC)]
    return StateStore(tmp_path / "state", now=lambda: clock[0]), clock


def row(store, name):
    return shelf.summary(store)[name]


def issues(store, *values):
    store.refresh_local_issues([Issue(number, f"Work {number}", "body", frozenset(labels), status)
                                for number, labels, status in values], [], {}, store._now())


def old_log(store, days=31):
    path = store.root / "logs" / "orphan.log"
    shelf_io.append_log(path, "evidence")
    at = (store._now() - timedelta(days=days)).timestamp()
    os.utime(path, (at, at))
    return path


def alarms(store):
    return [request for request in orch_inbox.requests(store) if request.get("kind") == "shelf-alarm"]


def test_read_only_summary_of_empty_store_creates_nothing(case):
    store, _ = case
    assert set(shelf.summary(store)) == {place.name for place in shelf.places()}
    assert all(value == {"count": 0, "in_24h": 0, "out_24h": 0, "oldest_minutes": None}
               for value in shelf.summary(store).values())
    assert shelf.report_alarms(store) == [] and shelf.pm_alarms(store) == []
    assert not store.root.exists()


def test_log_admission_append_and_removal_use_injected_clock_once(case):
    store, clock = case
    path = store.root / "logs" / "nested" / "output.log"
    shelf_io.append_log(path, "first")
    shelf_io.append_log(path, "second")
    assert path.read_text() == "first\nsecond\n"
    assert (row(store, "logs")["count"], row(store, "logs")["in_24h"]) == (1, 1)
    assert {event["at"] for event in shelf._read(store)["events"]} == {clock[0].isoformat()}
    shelf_io.remove(path)
    shelf_io.remove(path)
    assert row(store, "logs") == {"count": 0, "in_24h": 1, "out_24h": 1, "oldest_minutes": None}
    clock[0] += timedelta(hours=24)
    assert row(store, "logs")["in_24h"] == row(store, "logs")["out_24h"] == 0
    shelf.ask(store)
    assert shelf._read(store)["events"] == []


def test_subprocess_redirections_and_full_output_copies_are_counted(case):
    from foragent.cuts import save_full

    store, _ = case
    directory = store.root / "logs" / "nested"
    code, timed_out, output = run_logged(
        ["/bin/sh", "-c", "printf output; printf problem >&2"], cwd=store.root.parent,
        timeout_s=10, log_path=directory / "command.log", capture_stdout=True,
        stdout_path=directory / "stdout.log", stderr_path=directory / "stderr.log")
    assert (code, timed_out, output) == (0, False, "output")
    assert (directory / "stderr.log").read_text() == "problem"
    saved = save_full(directory, "full.txt", "whole output")
    assert saved.read_text() == "whole output"
    assert (row(store, "logs")["count"], row(store, "logs")["in_24h"]) == (4, 4)


def test_role_final_message_uses_the_log_writer_and_counts_its_admission(case):
    from foragent.orch_architect import _keep_message

    store, _ = case
    path = store.root / "logs" / "architect.log"
    assert _keep_message(path, " final answer ", "architect") == "final answer"
    assert path.read_text() == "\n--- architect final message ---\nfinal answer\n"
    assert (row(store, "logs")["count"], row(store, "logs")["in_24h"]) == (1, 1)


def test_concurrent_log_admissions_are_not_lost_or_doubled(case):
    store, _ = case
    path = store.root / "logs" / "shared.log"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda number: shelf_io.append_log(path, str(number)), range(12)))
    assert sorted(map(int, path.read_text().splitlines())) == list(range(12))
    assert (row(store, "logs")["count"], row(store, "logs")["in_24h"], row(store, "logs")["out_24h"]) == (1, 1, 0)


def test_short_lived_readers_do_not_drop_the_writers_clock(case):
    import gc

    store, clock = case
    peer = StateStore(store.root)
    shelf_io.append_log(store.log_path(1), "first")
    del peer
    gc.collect()
    clock[0] += timedelta(minutes=1)
    shelf_io.append_log(store.log_path(2), "second")
    events = shelf._read(store)["events"]
    assert [event["at"] for event in events] == [(clock[0] - timedelta(minutes=1)).isoformat(), clock[0].isoformat()]
    assert row(store, "logs")["in_24h"] == 2


def test_failed_atomic_replacement_keeps_content_and_has_no_extra_transition(case, monkeypatch):
    store, _ = case
    path = store.root / "runs" / "1.json"
    shelf_io.atomic_write(path, json.dumps({"started_at": store._timestamp()}))
    before = path.read_bytes(), shelf._read(store)

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(shelf_io.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        shelf_io.atomic_write(path, "replacement")
    assert (path.read_bytes(), shelf._read(store)) == before
    assert list(path.parent.glob("*.tmp")) == []


def test_replaced_draft_is_one_item_and_only_closed_issues_remove_it(case):
    store, clock = case
    issues(store, (1, [], "open"), (2, [], "closed"))
    shelf_io.keep_draft(store, 1, {"tests/test_first.py": b"first"})
    clock[0] += timedelta(minutes=4)
    shelf_io.keep_draft(store, 1, {"plan.json": b"{}", "session.log": b"evidence"})
    shelf_io.keep_draft(store, 2, {"plan.json": b"{}"})
    assert not (store.root / "drafts/1/tests").exists()
    assert row(store, "drafts") == {"count": 2, "in_24h": 2, "out_24h": 0, "oldest_minutes": 4}
    shelf_io.prune_drafts(store)
    shelf_io.prune_drafts(store)
    assert (store.root / "drafts/1/session.log").read_bytes() == b"evidence"
    assert not (store.root / "drafts/2").exists()
    assert row(store, "drafts") == {"count": 1, "in_24h": 2, "out_24h": 1, "oldest_minutes": 4}


def test_age_alarm_boundary_and_escalation_survive_a_new_store(case):
    store, clock = case
    path = old_log(store, days=30)
    shelf.ask(store)
    assert alarms(store) == []  # Equal is not over.
    clock[0] += timedelta(seconds=1)
    shelf.ask(store)
    assert [item["to"] for item in alarms(store)] == ["orchestrator"]
    clock[0] += timedelta(days=30)
    store = StateStore(store.root, now=lambda: clock[0])
    shelf.ask(store)
    assert [item["to"] for item in alarms(store)] == ["orchestrator"]
    clock[0] += timedelta(seconds=1)
    shelf.ask(store)
    assert [item["to"] for item in alarms(store)] == ["orchestrator", "pm"]
    assert len(shelf.pm_alarms(store)) == 1 and shelf.report_alarms(store) == []
    clock[0] += timedelta(days=30, seconds=1)
    shelf.ask(store)
    shelf.ask(store)
    assert shelf.report_alarms(store) == ["Shelf alarm: logs"]
    assert len(alarms(store)) == 2
    shelf_io.remove(path)
    shelf.ask(store)
    shelf.ask(store)
    assert shelf.pm_alarms(store) == shelf.report_alarms(store) == []
    assert [item["status"] for item in alarms(store)] == ["done", "done"]
    assert len(orch_events.events(store, "shelf-cleared")) == 1
    old_log(store)
    shelf.ask(store)
    assert [item["to"] for item in alarms(store)] == ["orchestrator", "pm", "orchestrator"]


def test_failed_alarm_delivery_does_not_spend_the_readers_time(case, monkeypatch):
    store, clock = case
    old_log(store)
    def fail(*args, **kwargs):
        raise OSError("box unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(orch_inbox, "add_request", fail)
        with pytest.raises(OSError, match="box unavailable"):
            shelf.ask(store)
    clock[0] += timedelta(days=31)
    shelf.ask(store)
    assert [item["to"] for item in alarms(store)] == ["orchestrator"]
    clock[0] += timedelta(days=30)
    shelf.ask(store)
    assert [item["to"] for item in alarms(store)] == ["orchestrator"]
    clock[0] += timedelta(seconds=1)
    shelf.ask(store)
    assert [item["to"] for item in alarms(store)] == ["orchestrator", "pm"]


def test_delivery_that_saved_then_failed_is_recovered_without_a_duplicate(case, monkeypatch):
    store, _ = case
    old_log(store)
    create = orch_inbox.add_request

    def save_then_fail(*args, **kwargs):
        create(*args, **kwargs)
        raise OSError("lost response")

    with monkeypatch.context() as patch:
        patch.setattr(orch_inbox, "add_request", save_then_fail)
        with pytest.raises(OSError, match="lost response"):
            shelf.ask(store)
    shelf.ask(store)
    shelf.ask(store)
    assert len(alarms(store)) == 1
    assert shelf._read(store)["episodes"]["logs"]["at"] == alarms(store)[0]["created_at"]


def test_held_count_includes_closed_slot_once_then_retry_and_release_leave(case):
    store, clock = case
    for number in range(1, 5):
        store.start_run(number, branch=f"issue-{number}", worktree=store.worktree_path(number))
    issues(store, (1, [], "closed"), (2, ["blocked"], "open"),
           (3, ["needs-human"], "open"), (4, ["running"], "open"))
    store.save_flag(2, "retries-exceeded", flagged_at=store._timestamp())
    shelf.observe(store)
    assert row(store, "held-runs") == {"count": 3, "in_24h": 3, "out_24h": 0, "oldest_minutes": 0}
    shelf.ask(store)
    assert alarms(store) == []
    store.finish_run(1, "stopped")
    store.record_labels(2, ["blocked"], remove=True, lanes={})
    clock[0] += timedelta(minutes=16)
    shelf.ask(store)
    assert row(store, "held-runs") == {"count": 1, "in_24h": 3, "out_24h": 2, "oldest_minutes": 16}
    assert [(item["to"], item["items"]) for item in alarms(store)] == [("pm", [3])]


def test_retention_removes_finished_records_without_logs_but_keeps_live_and_side_records(case):
    store, clock = case
    for number, outcome in ((1, "merged"), (2, "failed")):
        store.start_run(number, branch=f"issue-{number}", worktree=store.worktree_path(number))
        store.finish_run(number, outcome)
    store.start_run(3, branch="live", worktree=store.worktree_path(3))
    store.start_draft(4, "main", worktree=store.worktree_path(4))
    store.finish_run("draft-4", "drafted")
    clock[0] += timedelta(days=8)
    assert store.prune_logs(clock[0]) == []
    assert not (store.root / "runs/1.json").exists()
    assert store.get_run(2).outcome == "failed" and store.get_run(3).status == "running"
    clock[0] += timedelta(days=23)
    store.prune_logs(clock[0])
    store.prune_logs(clock[0])
    assert not (store.root / "runs/2.json").exists()
    assert store.get_run(3).status == "running" and store.get_draft(4).outcome == "drafted"
    assert (row(store, "run-records")["count"], row(store, "run-records")["out_24h"]) == (2, 1)
    assert len(store.read_history()) == 2  # Expiry removes snapshots, not completed facts.


def test_expiring_a_snapshot_does_not_resolve_its_unanswered_hold(case):
    store, clock = case
    store.start_run(1, branch="issue-1", worktree=store.worktree_path(1))
    store.finish_run(1, "retries-exceeded")
    issues(store, (1, ["blocked"], "open"))
    store.save_flag(1, "retries-exceeded", flagged_at=store._timestamp())
    shelf.observe(store)
    clock[0] += timedelta(days=31)
    store.prune_logs(clock[0])
    assert not (store.root / "runs/1.json").exists()
    shelf.observe(store)
    assert row(store, "held-runs") == {"count": 1, "in_24h": 0, "out_24h": 0,
                                        "oldest_minutes": 31 * 24 * 60}
    store.record_labels(1, ["blocked"], remove=True, lanes={})
    shelf.observe(store)
    assert row(store, "held-runs") == {"count": 0, "in_24h": 0, "out_24h": 1, "oldest_minutes": None}


def test_size_retention_removes_the_same_finished_record_as_its_log(case, monkeypatch):
    from foragent import state

    store, clock = case
    now = clock[0]
    for number, days in ((1, 6), (2, 5)):
        clock[0] = now - timedelta(days=days)
        store.start_run(number, branch=f"issue-{number}", worktree=store.worktree_path(number))
        store.finish_run(number, "merged")
        with shelf_io.open_file(store.log_path(number), "wb") as stream:
            stream.write(b"x" * 60)
    clock[0] = now
    monkeypatch.setattr(state, "LOG_LIMIT_BYTES", 100)
    assert store.prune_logs(now) == [store.log_path(1)]
    assert not store.log_path(1).exists() and store.log_path(2).is_file()
    assert store.get_run(1) is None and store.get_run(2).outcome == "merged"
    assert (row(store, "run-records")["count"], row(store, "run-records")["out_24h"]) == (1, 1)
    assert (row(store, "logs")["count"], row(store, "logs")["out_24h"]) == (1, 1)
