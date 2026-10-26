from dataclasses import asdict, dataclass, replace
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
import errno
import fcntl
import json
from multiprocessing import Pipe
from pathlib import Path

import pytest

from foragent.issue import body_hash
from foragent.state import StateStore
import foragent.state as state_module


NOW = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state", now=lambda: NOW)


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_update_run_keeps_recorded_ownership_when_process_table_is_unreadable(
    store, fake_process_table, tmp_path, monkeypatch, error,
):
    from foragent import processes

    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=11)
    run = store.start_run(1, branch="branch", worktree=tmp_path, pid=root.pid)
    child = table.add(42002, ppid=root.pid, cwd=tmp_path, start_time=12)
    birth = {"pid": child.pid, "start_time": child.start_time}

    def unavailable():
        raise error("fake process table unavailable")

    monkeypatch.setattr(processes, "list_processes", unavailable)
    updated = store.update_run(1, detail="Stop refused: ownership unreadable", process_identities=[birth])
    assert updated.detail == "Stop refused: ownership unreadable"
    assert updated.process_identities == [*run.process_identities, birth]
    assert store.get_run(1) == updated
    assert updated.status == "running"
    assert not store.read_history() and not table.signals


@pytest.mark.parametrize("invalid", [
    {"node_ids": "passing"},
    {"collected_ids": []},
    {"failing_ids": ["passing"]},
    {"failing_ids": ["absent"]},
    {"excluded": "tests/acceptance/test_pending.py"},
])
def test_acceptance_baseline_cache_rejects_inconsistent_coverage(store, invalid):
    saved = {
        "created_at": NOW.isoformat(), "node_ids": ["passing"],
        "collected_ids": ["passing", "failing", "skipped"], "failing_ids": ["failing"],
        "excluded": ["tests/acceptance/test_pending.py"],
    }
    store._write_json(store.root / "baseline.json", saved)
    assert store.load_acceptance_baseline("baseline") == saved
    store._write_json(store.root / "baseline.json", {**saved, **invalid})
    assert store.load_acceptance_baseline("baseline") is None


def _mutate_run(root, method, number, fields, connection, pause_read):
    from foragent import processes

    processes.list_processes = lambda: ()  # PIDs here are serialization fixtures.
    store = StateStore(root)
    if pause_read:
        original_get = store.get_run

        def paused_get(number):
            run = original_get(number)
            connection.send("read")
            if not connection.poll(10):
                raise TimeoutError("mutation was not resumed")
            assert connection.recv() == "continue"
            return run

        store.get_run = paused_get
    try:
        connection.send("started")
        connection.send(getattr(store, method)(number, **fields))
    finally:
        connection.close()


@pytest.fixture
def mutation_process():
    # Independent flock descriptors contend in threads just as in processes;
    # never launch or terminate real processes to exercise state serialization.
    pool = ThreadPoolExecutor()
    children = []

    def start(root, method, number=1, *, pause_read=False, **fields):
        parent, child = Pipe()
        process = pool.submit(_mutate_run, root, method, number, fields, child, pause_read)
        children.append((process, parent))
        assert parent.poll(10)
        assert parent.recv() == "started"
        return parent

    yield start
    for process, connection in children:
        process.result(timeout=15)
        connection.close()
    pool.shutdown()


def test_repository_root_and_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store = StateStore.for_repo("owner/name")
    assert store.root == tmp_path / ".local/share/foragent/owner__name"
    assert store.worktree_path(42) == store.root / "worktrees/issue-42"
    assert store.log_path(42) == store.root / "logs/issue-42.log"
    assert not store.root.exists()


def test_flags_persist_latest_reason_and_timestamp_per_issue(store):
    assert store.load_flag(1) is None
    store.save_flag(1, "hash-mismatch")
    reopened = StateStore(store.root)
    first = reopened.load_flag(1)
    assert first.number == 1 and first.reason == "hash-mismatch" and first.flagged_at == NOW.isoformat()
    later = (NOW + timedelta(minutes=1)).isoformat()
    store.save_flag(2, "spec-question")
    store.save_flag(1, "retries-exceeded", flagged_at=later)
    assert reopened.load_flag(1).reason == "retries-exceeded"
    assert reopened.load_flag(1).flagged_at == later
    assert reopened.load_flag(2).reason == "spec-question"


def test_restart_budget_survives_crash_recovery_but_resets_after_completion(store):
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    store.update_run(1, restarts=1)
    store.finish_run(1, "stalled-restart")
    assert store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1)).restarts == 1
    store.finish_run(1, "crashed")
    assert store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1)).restarts == 1
    store.finish_run(1, "merged")
    assert store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1)).restarts == 0


def test_recovery_metadata_survives_restart_and_old_records_default_to_empty(store):
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, merge_conflicts=["notes.txt"], local_skill_paths=[".claude/skills/testing"])
    store.finish_run(1, "timeout")
    reopened = StateStore(store.root)
    run = reopened.start_run(1, branch="branch", worktree="worktree")
    assert run.merge_conflicts == ["notes.txt"]
    assert run.local_skill_paths == [".claude/skills/testing"]
    path = store.root / "runs/1.json"
    old_run = json.loads(path.read_text())
    del old_run["merge_conflicts"], old_run["local_skill_paths"]
    path.write_text(json.dumps(old_run))
    (store.root / "runs.jsonl").write_text(json.dumps(old_run) + "\n")
    for run in (reopened.get_run(1), *reopened.read_history()):
        assert run.merge_conflicts == [] and run.local_skill_paths == []


def test_run_approval_hash_is_pinned_until_next_execution(store):
    assert store.start_run(1, branch="branch", worktree="worktree").approval_body_sha is None
    store.finish_run(1, "no-approval")
    store.save_approval(1, body="original", body_sha=body_hash("original"), acceptance_blobs={},
                        approved_by="human", approved_at=NOW.isoformat())
    original = store.load_approval(1)
    store.start_run(1, branch="branch", worktree="worktree")
    store.save_approval(1, body="replacement", body_sha=body_hash("replacement"), acceptance_blobs={},
                        approved_by="human", approved_at=(NOW + timedelta(seconds=1)).isoformat())
    store.update_run(1, pid=7001)
    reopened = StateStore(store.root, now=lambda: NOW + timedelta(seconds=2))
    assert reopened.get_run(1).approval_body_sha == original.body_sha
    reopened.finish_run(1, "crashed")
    assert reopened.read_history()[-1].approval_body_sha == original.body_sha
    run = reopened.start_run(1, branch="branch", worktree="worktree")
    assert run.approval_body_sha == reopened.load_approval(1).body_sha != original.body_sha


def test_legacy_run_and_history_do_not_infer_approval_hash(store):
    store.save_approval(1, body="approved", body_sha=body_hash("approved"), acceptance_blobs={},
                        approved_by="human", approved_at=NOW.isoformat())
    store.start_run(1, branch="branch", worktree="worktree")
    store.finish_run(1, "merged")
    path = store.root / "runs/1.json"
    record = json.loads(path.read_text())
    del record["approval_body_sha"]
    path.write_text(json.dumps(record))
    (store.root / "runs.jsonl").write_text(json.dumps(record) + "\n")
    reopened = StateStore(store.root)
    assert reopened.get_run(1).approval_body_sha is None
    assert reopened.read_history()[0].approval_body_sha is None


def test_limit_wait_resumptions_preserve_attempts_and_restarts(store):
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, attempts=2, restarts=1, waited_seconds=120, executor_waited_seconds=120)
    for _ in range(3):
        store.finish_run(1, "limit-wait")
        run = store.start_run(1, branch="branch", worktree="worktree", lane="ui")
        assert (run.attempts, run.restarts, run.lane) == (2, 1, "ui")
        assert run.waited_seconds == 0 and run.outcome is None and run.status == "running"
        assert run.executor_waited_seconds == 0
    store.finish_run(1, "merged")
    run = store.start_run(1, branch="branch", worktree="worktree")
    assert (run.attempts, run.restarts) == (1, 0)


def test_github_pause_is_persisted_project_wide_and_cannot_be_shortened(store):
    until = NOW + timedelta(minutes=10)
    assert store.pause_github(until) == until
    reopened = StateStore(store.root)
    assert reopened.pause_github(NOW + timedelta(minutes=1)) == until
    assert store.github_wait_until(until - timedelta(seconds=1)) == until
    assert reopened.github_wait_until(until) is None
    assert not store.lane_paused("main", NOW)


def test_executor_wait_counter_persists_separately_and_old_runs_default_to_zero(store):
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, waited_seconds=1980, executor_waited_seconds=120)
    reopened = StateStore(store.root)
    assert reopened.get_run(1).waited_seconds == 1980
    assert reopened.get_run(1).executor_waited_seconds == 120
    store.finish_run(1, "merged")
    assert reopened.read_history()[0].executor_waited_seconds == 120
    path = store.root / "runs/1.json"
    old_run = json.loads(path.read_text())
    del old_run["executor_waited_seconds"]
    path.write_text(json.dumps(old_run))
    (store.root / "runs.jsonl").write_text(json.dumps(old_run) + "\n")
    assert reopened.get_run(1).executor_waited_seconds == 0
    assert reopened.read_history()[0].executor_waited_seconds == 0


def test_run_visibility_fields_survive_completion_and_old_records_still_load(store):
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, phase="checking", phase_started_at=NOW.isoformat())
    store.finish_run(1, "merged")
    reopened = StateStore(store.root)
    assert reopened.get_run(1).phase == reopened.read_history()[0].phase == "checking"
    assert reopened.read_history()[0].last_activity_at == NOW.isoformat()
    path = store.root / "runs/1.json"
    legacy = json.loads(path.read_text())
    for name in ("phase", "phase_started_at", "last_activity_at"):
        del legacy[name]
    path.write_text(json.dumps(legacy))
    (store.root / "runs.jsonl").write_text(json.dumps(legacy) + "\n")
    for run in (reopened.get_run(1), *reopened.read_history()):
        assert run.phase is None and run.phase_started_at is None and run.last_activity_at is None


def _acquire_merge_lock(root, connection):
    try:
        connection.send("waiting")
        with StateStore(root).merge_lock():
            connection.send("acquired")
            assert connection.poll(10)
            assert connection.recv() == "release"
    finally:
        connection.close()


def test_project_merge_lock_blocks_other_processes_and_releases_on_error(store):
    parent, child = Pipe()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with pytest.raises(RuntimeError, match="release lock"):
            with store.merge_lock():
                # State mutations and scheduler ticks use separate locks.
                store.start_run(1, branch="branch", worktree="worktree")
                with store.tick_lock() as acquired:
                    assert acquired
                process = pool.submit(_acquire_merge_lock, store.root, child)
                assert parent.poll(10) and parent.recv() == "waiting"
                assert not parent.poll(0.1)
                raise RuntimeError("release lock")
        assert parent.poll(10) and parent.recv() == "acquired"
        parent.send("release")
        process.result(timeout=15)
    finally:
        pool.shutdown()
        parent.close()
        child.close()


def test_approval_exact_body_and_metadata_round_trip(store):
    body = "  <!-- foragent:v1 -->\r\n## Goal\r\nKeep café text.  \r\n\r\n"
    values = {
        "body": body, "body_sha": body_hash(body),
        "acceptance_blobs": {"tests/acceptance/test_a.py": "d" * 40},
        "approved_by": "human", "approved_at": NOW.isoformat(),
    }
    assert store.load_approval(42) is None
    store.save_approval(42, **values)
    assert asdict(store.load_approval(42)) == {"number": 42, **values}
    snapshot = json.loads((store.root / "approvals/42.json").read_text(encoding="utf-8"))
    assert snapshot == {"number": 42, **values}
    assert [path.name for path in (store.root / "approvals").iterdir()] == ["42.json"]
    store.save_approval(42, **{**values, "body": "replacement"})
    assert store.load_approval(42).body == "replacement"
    store.clear_approval(42)
    store.clear_approval(42)
    assert store.load_approval(42) is None
    assert list((store.root / "approvals").iterdir()) == []


def test_failed_approval_replacement_preserves_complete_snapshot(store, monkeypatch):
    store.save_approval(1, body="original", body_sha="old-sha", acceptance_blobs={"check.sh": "old"},
                        approved_by="first", approved_at=NOW.isoformat())
    original = store.load_approval(1)
    original_replace = state_module.os.replace

    def fail_snapshot_replace(source, destination):
        if Path(destination) == store.root / "approvals/1.json":
            raise OSError(errno.ENOSPC, "No space left on device")
        original_replace(source, destination)

    monkeypatch.setattr(state_module.os, "replace", fail_snapshot_replace)
    with pytest.raises(OSError, match="No space left"):
        store.save_approval(1, body="replacement", body_sha="new-sha", acceptance_blobs={"check.sh": "new"},
                            approved_by="second", approved_at=(NOW + timedelta(minutes=1)).isoformat())
    assert store.load_approval(1) == original
    assert list(store.root.rglob("*.tmp")) == []


def test_approval_readers_observe_complete_snapshots_during_replacement(store, monkeypatch):
    store.save_approval(1, body="original", body_sha="old-sha", acceptance_blobs={"check.sh": "old"},
                        approved_by="first", approved_at=NOW.isoformat())
    reader = StateStore(store.root)
    original = reader.load_approval(1)
    original_replace = state_module.os.replace
    observed = []

    def observe_replace(source, destination):
        observed.append(reader.load_approval(1))
        original_replace(source, destination)
        observed.append(reader.load_approval(1))

    monkeypatch.setattr(state_module.os, "replace", observe_replace)
    store.save_approval(1, body="replacement", body_sha="new-sha", acceptance_blobs={"check.sh": "new"},
                        approved_by="second", approved_at=(NOW + timedelta(minutes=1)).isoformat())
    replacement = reader.load_approval(1)
    assert observed == [original, replacement]


def test_runs_round_trip_update_active_and_history(store):
    assert store.get_run(1) is None
    assert store.active_runs() == []
    assert store.read_history() == []
    first = store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1), pid=123)
    assert first.number == 1
    assert first.branch == "foragent/issue-1"
    assert first.worktree == str(store.worktree_path(1))
    assert first.pid == 123
    assert first.started_at == first.last_progress_at == NOW.isoformat()
    assert (first.attempts, first.restarts, first.status, first.outcome, first.detail) == (1, 0, "running", None, "")
    assert store.get_run(1) == first
    assert StateStore(store.root).get_run(1) == first
    progress = (NOW + timedelta(minutes=5)).isoformat()
    updated = store.update_run(1, attempts=2, restarts=1, pid=456, last_progress_at=progress)
    assert updated.last_progress_at == progress
    assert updated.started_at == first.started_at
    assert (updated.attempts, updated.restarts, updated.pid) == (2, 1, 456)
    store.start_run(10, branch="ten", worktree="/work/ten")
    store.start_run(2, branch="two", worktree="/work/two")
    assert [run.number for run in store.active_runs()] == [1, 2, 10]
    finished = store.finish_run(1, "success", "Checks passed.")
    assert finished.status == "finished"
    assert finished.outcome == "success"
    assert finished.detail == "Checks passed."
    assert store.get_run(1) == finished
    store.finish_run(2, "failed")
    assert [run.number for run in store.active_runs()] == [10]
    history = [json.loads(line) for line in (store.root / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    # Each history entry also saves when the run finished, beside the Run fields.
    ended = store._timestamp()
    assert history == [{**asdict(finished), "finished_at": ended}, {**asdict(store.get_run(2)), "finished_at": ended}]
    assert store.finish_times() == {(1, finished.started_at): ended, (2, store.get_run(2).started_at): ended}
    assert store.read_history() == [finished, store.get_run(2)]
    assert store.finish_run(1, "success") == finished
    assert len((store.root / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_new_execution_of_same_issue_adds_history(store):
    store.start_run(1, branch="first", worktree="first")
    first = store.finish_run(1, "failed")
    store._now = lambda: NOW + timedelta(minutes=1)
    store.start_run(1, branch="second", worktree="second")
    second = store.finish_run(1, "success")
    history = [json.loads(line) for line in (store.root / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [run["outcome"] for run in history] == ["failed", "success"]
    assert store.read_history() == [first, second]


@pytest.mark.parametrize("fields", [{"pid": 999}, {"status": "running", "outcome": None}])
def test_finished_runs_ignore_updates(store, fields):
    store.start_run(1, branch="branch", worktree="worktree", pid=123)
    finished = store.finish_run(1, "success")
    assert store.update_run(1, **fields) == finished
    assert store.get_run(1) == finished
    assert store.read_history() == [finished]


def test_failed_history_write_can_be_retried(store, monkeypatch):
    running = store.start_run(1, branch="branch", worktree="worktree")
    atomic_write = store._atomic_write

    def fail_history_write(path, content):
        if path == store.root / "runs.jsonl":
            raise OSError("history write failed")
        atomic_write(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_atomic_write", fail_history_write)
        with pytest.raises(OSError, match="history write failed"):
            store.finish_run(1, "success", "Checks passed.")
    assert store.get_run(1) == running
    assert store.read_history() == []
    finished = store.finish_run(1, "success", "Checks passed.")
    assert store.get_run(1) == finished
    assert store.read_history() == [finished]


def test_failed_finish_record_write_can_be_retried_without_duplicate_history(store, monkeypatch):
    running = store.start_run(1, branch="branch", worktree="worktree")
    write_json = store._write_json

    def fail_record_write(path, value):
        if path == store.root / "runs/1.json":
            raise OSError("record write failed")
        write_json(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_write_json", fail_record_write)
        with pytest.raises(OSError, match="record write failed"):
            store.finish_run(1, "success", "Checks passed.")
    assert store.get_run(1) == running
    assert len(store.read_history()) == 1
    finished = store.finish_run(1, "success", "Checks passed.")
    assert store.get_run(1) == finished
    assert len((store.root / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert store.read_history() == [finished]
    assert store.finish_run(1, "failed") == finished
    assert len((store.root / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_history_deduplicates_by_number_and_start_time_using_latest_entry(store):
    store.start_run(1, branch="one", worktree="one")
    first = store.finish_run(1, "success")
    store.start_run(2, branch="two", worktree="two")
    second = store.finish_run(2, "failed")
    latest = {**asdict(first), "detail": "Retried completion"}
    with (store.root / "runs.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(latest) + "\n")
    history = store.read_history()
    assert [asdict(run) for run in history] == [latest, asdict(second)]


@pytest.mark.parametrize("method,args", [("update_run", {"pid": 1}), ("finish_run", {"outcome": "failed"})])
def test_missing_run_mutation_is_an_error(store, method, args):
    with pytest.raises(KeyError):
        getattr(store, method)(99, **args)


def test_unknown_update_field_does_not_change_record(store):
    run = store.start_run(1, branch="branch", worktree="worktree")
    with pytest.raises(TypeError):
        store.update_run(1, unsupported=True)
    assert store.get_run(1) == run


def test_timestamps_are_utc_even_with_injected_offset(tmp_path):
    offset_now = datetime(2026, 10, 20, 18, 30, tzinfo=timezone(timedelta(hours=9)))
    store = StateStore(tmp_path, now=lambda: offset_now)
    run = store.start_run(1, branch="branch", worktree="worktree")
    assert run.started_at == NOW.isoformat()
    assert datetime.fromisoformat(run.started_at).utcoffset() == timedelta(0)


def test_naive_clock_is_rejected(tmp_path):
    store = StateStore(tmp_path, now=lambda: NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.start_run(1, branch="branch", worktree="worktree")


def test_all_writes_replace_a_complete_same_directory_temp_file(store, monkeypatch):
    original_replace = state_module.os.replace
    destinations = []

    def inspect_replace(source, destination):
        source, destination = Path(source), Path(destination)
        assert source.parent == destination.parent
        assert source.is_file()
        assert source.read_text(encoding="utf-8")
        destinations.append(destination.relative_to(store.root).as_posix())
        original_replace(source, destination)

    monkeypatch.setattr(state_module.os, "replace", inspect_replace)
    store.save_approval(1, body="body", body_sha="sha", acceptance_blobs={}, approved_by="human", approved_at=NOW.isoformat())
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, attempts=2)
    store.finish_run(1, "success")
    assert destinations == ["approvals/1.json", "runs/1.json", "runs/1.json", "runs.jsonl", "runs/1.json"]
    assert list(store.root.rglob("*.tmp")) == []


def test_failed_replace_preserves_previous_record_and_removes_temp(store, monkeypatch):
    original = store.start_run(1, branch="branch", worktree="worktree")

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(state_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.update_run(1, attempts=2)
    assert store.get_run(1) == original
    assert list(store.root.rglob("*.tmp")) == []


def test_outbox_refresh_preserves_issue_created_after_bulk_read_started(store):
    from foragent.tracker import Issue

    revision = store.issues_revision()
    issue = Issue(1, "New issue", "body", frozenset(), "open")
    store.record_issue(issue, {})
    store.queue_github_write(1, "comment", body="New evidence")
    pending = store.github_outbox()

    assert store.refresh_local_issues([], [], {}, NOW, since_revision=revision) == []
    assert store.github_outbox() == pending
    assert store.local_issues()[0]["number"] == 1


@pytest.mark.parametrize("method,fields", [
    ("start_run", {"branch": "new", "worktree": "new"}),
    ("update_run", {"pid": 999}),
    ("finish_run", {"outcome": "success"}),
])
def test_run_mutations_block_on_state_lock_in_another_process(store, mutation_process, method, fields):
    store.start_run(1, branch="branch", worktree="worktree")
    with (store.root / "state.lock").open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        connection = mutation_process(store.root, method, **fields)
        assert not connection.poll(0.2)
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    assert connection.poll(10)
    assert connection.recv() == store.get_run(1)


@pytest.mark.parametrize("first_method,first_fields,second_method,second_fields", [
    ("update_run", {"pid": 999}, "finish_run", {"outcome": "success"}),
    ("finish_run", {"outcome": "success"}, "update_run", {"pid": 999, "status": "running"}),
])
def test_competing_update_and_finish_are_serialized(
    store, mutation_process, first_method, first_fields, second_method, second_fields,
):
    store.start_run(1, branch="branch", worktree="worktree", pid=123)
    first = mutation_process(store.root, first_method, pause_read=True, **first_fields)
    assert first.poll(10)
    assert first.recv() == "read"
    second = mutation_process(store.root, second_method, **second_fields)
    try:
        assert not second.poll(0.2)
    finally:
        first.send("continue")
    assert first.poll(10)
    first_result = first.recv()
    assert second.poll(10)
    finished = second.recv()
    assert finished.status == "finished"
    assert finished.pid == (999 if first_method == "update_run" else 123)
    assert store.get_run(1) == finished
    assert store.read_history() == [finished]
    if first_method == "finish_run":
        assert first_result == finished


def test_finishes_in_separate_processes_preserve_both_history_entries(store, mutation_process):
    store.start_run(1, branch="one", worktree="one")
    store.start_run(2, branch="two", worktree="two")
    first = mutation_process(store.root, "finish_run", pause_read=True, outcome="success")
    assert first.poll(10)
    assert first.recv() == "read"
    second = mutation_process(store.root, "finish_run", number=2, outcome="failed")
    try:
        assert not second.poll(0.2)
    finally:
        first.send("continue")
    assert first.poll(10)
    first_result = first.recv()
    assert second.poll(10)
    second_result = second.recv()
    assert store.read_history() == [first_result, second_result]


def test_run_mutations_work_while_tick_lock_is_held(store):
    with store.tick_lock() as acquired:
        assert acquired
        store.start_run(1, branch="branch", worktree="worktree")
        store.update_run(1, pid=999)
        finished = store.finish_run(1, "success")
    assert store.read_history() == [finished]


def test_tick_lock_second_file_descriptor_never_blocks(store):
    second = StateStore(store.root)
    with store.tick_lock() as acquired:
        assert acquired is True
        with second.tick_lock() as other_acquired:
            assert other_acquired is False
        with store.tick_lock() as nested_acquired:
            assert nested_acquired is False
    with second.tick_lock() as acquired:
        assert acquired is True


def test_tick_lock_released_on_exception(store):
    with pytest.raises(RuntimeError, match="interrupted"):
        with store.tick_lock() as acquired:
            assert acquired
            raise RuntimeError("interrupted")
    with store.tick_lock() as acquired:
        assert acquired


def test_issue_snapshot_is_shared_and_remote_refresh_replaces_local_labels(store):
    from foragent.tracker import Issue

    issue = Issue(1, "Feature", "body", frozenset({"approved"}), "open")
    assert store.local_issues() == []
    assert store.issues_refresh_due(NOW)
    store.refresh_local_issues([issue], [], {}, NOW)
    other = StateStore(store.root, now=lambda: NOW)
    other.record_labels(1, ["ready"], remove=False, lanes={})
    assert store.local_issues()[0]["labels"] == ["approved", "ready"]
    assert not other.issues_refresh_due(NOW + timedelta(minutes=5, microseconds=-1))
    assert other.issues_refresh_due(NOW + timedelta(minutes=5))
    human = replace(issue, title="Human title", labels=frozenset({"needs-human"}), state="closed")
    other.refresh_local_issues([human], [], {}, NOW + timedelta(minutes=5))
    row, = store.local_issues()
    assert (row["title"], row["labels"], row["state"]) == ("Human title", ["needs-human"], "closed")


def test_outbox_refresh_uses_first_queued_write_and_drops_only_changed_issue(store):
    from foragent.tracker import Issue

    issues = [Issue(number, "Feature", "body", frozenset({"blocked"}), "open", updated_at=NOW.isoformat())
              for number in (1, 2)]
    store.refresh_local_issues(issues, [], {}, NOW)
    for issue in issues:
        store.queue_github_write(issue.number, "add_labels", labels=["ready"])
    first = store.github_outbox()
    assert store.refresh_local_issues(issues, [], {}, NOW) == []
    assert all(row["labels"] == ["blocked", "ready"] for row in store.local_issues())
    assert store.github_outbox() == first

    revision = store.issues_revision()
    later = StateStore(store.root, now=lambda: NOW + timedelta(minutes=2))
    later.queue_github_write(1, "comment", body="Pending evidence")
    # The person's edit predates the later comment, but follows the first queued write.
    changed = replace(issues[0], labels=frozenset({"needs-human"}),
                      updated_at=(NOW + timedelta(minutes=1)).isoformat())
    assert later.refresh_local_issues([changed, issues[1]], [], {}, NOW + timedelta(minutes=3),
                                      since_revision=revision) == [1]
    assert later.github_outbox() == [first[1]]
    row, untouched = later.local_issues()
    assert row["labels"] == ["needs-human"] and row["comments"] == []
    assert untouched["labels"] == ["blocked", "ready"]


@pytest.mark.parametrize("method,fields", [
    ("update_local_issue", {"state": "closed", "pr_number": 42, "merged": True}),
    ("record_labels", {"labels": ["blocked", "needs-human"], "remove": True, "lanes": {}}),
    ("record_comments", {"comments": [{"id": 1, "body": "New local evidence"}]}),
    ("record_board_value", {"project": "project", "field": "Status", "value": "Todo"}),
])
def test_refresh_preserves_each_kind_of_concurrent_local_write(store, method, fields):
    from foragent.tracker import Issue, PullRequest

    issue = Issue(1, "Question", "body", frozenset({"blocked", "needs-human"}), "open")
    store.refresh_local_issues([issue], [], {}, NOW)
    revision = store.issues_revision()
    writer = StateStore(store.root, now=lambda: NOW)
    getattr(writer, method)(1, **fields)
    written, = writer.local_issues()
    stale_pr = PullRequest(99, "foragent/issue-1", "main", "Old PR", "Old body")
    store.refresh_local_issues([issue], [stale_pr], {}, NOW,
                               comments={1: []}, since_revision=revision)
    assert store.local_issues() == [written]

    revision = store.issues_revision()
    human = replace(issue, title="Human revision", labels=frozenset({"needs-human"}), state="closed")
    store.refresh_local_issues([human], [], {}, NOW + timedelta(minutes=5), since_revision=revision)
    row, = writer.local_issues()
    assert (row["title"], row["labels"], row["state"]) == ("Human revision", ["needs-human"], "closed")


def test_confirmed_read_needs_a_later_listing_with_the_same_issue_and_link_facts(store):
    from foragent.tracker import Issue

    issue = Issue(1, "Feature", "body", frozenset({"approved"}), "open", updated_at=NOW.isoformat())
    blocker = Issue(2, "Blocker", "body", frozenset(), "open", updated_at=NOW.isoformat())
    store.refresh_local_issues([issue, blocker], [], {}, NOW)
    revision = store.issues_revision()
    store.record_read(1, "blocked_by", [{"number": 2}], baseline=store.read_baseline(), links=[2])
    assert store.issues_revision() == revision, "A remembered read is not an issue change"
    assert store.confirmed_read(1, "blocked_by") is None, "No listing after the read confirms it yet"
    store.refresh_local_issues([issue, blocker], [], {}, NOW + timedelta(minutes=5))
    assert store.confirmed_read(1, "blocked_by") == ([{"number": 2}],)
    closed = replace(blocker, state="closed", updated_at=(NOW + timedelta(minutes=6)).isoformat())
    store.refresh_local_issues([issue, closed], [], {}, NOW + timedelta(minutes=10))
    assert store.confirmed_read(1, "blocked_by") is None, "A changed blocker voids its dependent's read"


@pytest.mark.parametrize("change", ["updated_at", "local_label", "pr"])
def test_confirmed_read_is_void_after_any_issue_change(store, change):
    from foragent.tracker import Issue, PullRequest

    issue = Issue(1, "Feature", "body", frozenset({"approved"}), "open", updated_at=NOW.isoformat())
    store.refresh_local_issues([issue], [], {}, NOW, complete_prs=True)
    store.record_read(1, "label_events", [], baseline=store.read_baseline())
    prs = []
    if change == "updated_at":
        issue = replace(issue, updated_at=(NOW + timedelta(minutes=1)).isoformat())
    elif change == "local_label":
        store.record_labels(1, ["ready"], remove=False, lanes={})
    else:
        prs = [PullRequest(7, "foragent/issue-1", "main", "PR", "")]
    store.refresh_local_issues([issue], prs, {}, NOW + timedelta(minutes=5), complete_prs=True)
    assert store.confirmed_read(1, "label_events") is None


def test_read_baseline_taken_before_a_concurrent_refresh_keeps_a_stale_read_unconfirmed(store):
    from foragent.tracker import Issue

    issue = Issue(1, "Feature", "body", frozenset({"approved"}), "open", updated_at=NOW.isoformat())
    store.refresh_local_issues([issue], [], {}, NOW)
    baseline = store.read_baseline()
    # Another process refreshes while the read is in flight and sees a change.
    changed = replace(issue, updated_at=(NOW + timedelta(minutes=1)).isoformat())
    store.refresh_local_issues([changed], [], {}, NOW + timedelta(minutes=1))
    store.record_read(1, "label_events", ["value read before the change"], baseline=baseline)
    store.refresh_local_issues([changed], [], {}, NOW + timedelta(minutes=6))
    assert store.confirmed_read(1, "label_events") is None


@pytest.mark.parametrize("link", ["other/repo#2", 3])
def test_read_linked_to_an_issue_outside_this_snapshot_is_never_confirmed(store, link):
    from foragent.tracker import Issue

    issues = [Issue(number, "Feature", "body", frozenset(), "open", updated_at=NOW.isoformat()) for number in (1, 2)]
    store.refresh_local_issues(issues, [], {}, NOW)
    store.record_read(1, "blocked_by", [], baseline=store.read_baseline(), links=[link])
    store.refresh_local_issues(issues, [], {}, NOW + timedelta(minutes=5))
    assert store.confirmed_read(1, "blocked_by") is None


@pytest.mark.parametrize("field", ["sub_issues_summary", "dependencies_summary"])
def test_link_count_change_alone_voids_a_read(store, field):
    from foragent.tracker import Issue

    issue = Issue(1, "Feature", "body", frozenset(), "open", updated_at=NOW.isoformat(),
                  **{field: (("total", 1),)})
    store.refresh_local_issues([issue], [], {}, NOW)
    store.record_read(1, "blocked_by", [], baseline=store.read_baseline())
    store.refresh_local_issues([replace(issue, **{field: (("total", 0),)})], [], {}, NOW + timedelta(minutes=5))
    assert store.confirmed_read(1, "blocked_by") is None


def test_facade_globals_patched_on_foragent_state_reach_the_split_implementation(store, monkeypatch):
    store.refresh_local_issues([], [], {}, NOW)
    assert not store.issues_refresh_due(NOW)
    monkeypatch.setattr(state_module, "REFRESH_INTERVAL", timedelta(0))
    assert store.issues_refresh_due(NOW)

    built = []
    original_run = state_module.Run

    @dataclass(frozen=True)
    class RecordingRun(original_run):
        def __post_init__(self):
            built.append(self.number)

    monkeypatch.setattr(state_module, "Run", RecordingRun)
    store.start_run(3, branch="foragent/issue-3", worktree=store.worktree_path(3))
    assert built == [3]
    assert store.get_run(3).branch == "foragent/issue-3"
    assert built == [3, 3]

    facts = []
    monkeypatch.setattr(state_module, "observed_issue_facts", lambda issue: facts.append(issue) or {})
    issue = type("Issue", (), {"number": 5, "title": "t", "body": "b", "labels": [], "state": "open",
                                "updated_at": None, "is_pull_request": False})()
    store.refresh_local_issues([issue], [], {}, NOW)
    assert facts == [issue]


def test_merge_facts_keep_unmerged_runs_null_and_reset_on_a_new_record(store):
    store.start_run(1, branch="branch", worktree="worktree")
    store.update_run(1, last_review_pass_at=NOW.isoformat())
    store.record_merge_lock(1, 7)
    run = store.get_run(1)
    assert run.review_pass_to_merge_seconds is None
    assert run.merge_lock_held_seconds is None
    store.record_merge(1, NOW + timedelta(seconds=20))
    store.record_merge_lock(1, 3)
    store.finish_run(1, "merged")
    saved = json.loads((store.root / "runs/1.json").read_text())
    assert saved["review_pass_to_merge_seconds"] == 20
    assert saved["merge_lock_held_seconds"] == 10
    restarted = store.start_run(1, branch="branch", worktree="worktree")
    assert restarted.last_review_pass_at is None
    assert restarted.review_pass_to_merge_seconds is None
    assert restarted.merge_lock_held_seconds is None
    store.record_merge(1, NOW)
    store.record_merge_lock(1, 2)
    assert store.get_run(1).review_pass_to_merge_seconds is None
    assert store.get_run(1).merge_lock_held_seconds == 2
