"""Completion is committed once; the per-issue snapshot is a compatibility view."""

from datetime import UTC, datetime, timedelta
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from foragent.state import StateStore


NOW = datetime(2026, 11, 4, tzinfo=UTC)
DETAIL = "Command: " + "test_node " * 150 + "Cause: protected check timed out."
INPUT_DETAIL = "Command: " + "test_node " * 150 + "\r\n\tCause: protected check timed out."


def start(root):
    store = StateStore(root, now=lambda: NOW)
    return store, store.start_run(1, branch="branch", worktree=root / "worktree")


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("after_replace", [False, True])
def test_every_finish_write_boundary_survives_reopening(tmp_path, monkeypatch, legacy, after_replace):
    real_replace = os.replace
    for fail_at in range(1, 5):
        store, _ = start(tmp_path / str(fail_at))
        if legacy:
            path = store.root / "runs/1.json"
            value = json.loads(path.read_text())
            value.pop("run_generation")
            value["future_metadata"] = {"keep": [1, 2]}
            path.write_text(json.dumps(value))
        writes = []

        def interrupted(source, destination):
            relevant = Path(destination).is_relative_to(store.root)
            if relevant:
                writes.append(destination)
                if len(writes) == fail_at and not after_replace:
                    raise OSError("crashed before commit")
            real_replace(source, destination)
            if relevant and len(writes) == fail_at and after_replace:
                raise OSError("crashed after commit")

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", interrupted)
            try:
                store.finish_run(1, "failed", INPUT_DETAIL)
            except OSError:
                pass
        reopened = StateStore(store.root, now=lambda: NOW)
        latest, history = reopened.get_run(1), reopened.read_history()
        if latest.status == "finished":
            assert history == [latest]
            assert latest.detail == DETAIL
            assert reopened.active_runs() == []
            before = (store.root / "runs.jsonl").read_bytes()
            assert reopened.finish_run(1, "wrong", "wrong") == latest
            assert (store.root / "runs.jsonl").read_bytes() == before
            if legacy:
                assert json.loads(before)["future_metadata"] == {"keep": [1, 2]}
        else:
            assert latest.status == "running" and history == []
            finished = reopened.finish_run(1, "failed", INPUT_DETAIL)
            assert reopened.get_run(1) == finished
            assert reopened.read_history() == [finished]


def test_restart_on_same_clock_is_new_and_does_not_inherit_its_completion(tmp_path):
    store, first = start(tmp_path)
    store.update_run(1, detail=INPUT_DETAIL)
    store.finish_run(1, "limit-wait")
    previous_generation = json.loads((tmp_path / "runs/1.json").read_text())["run_generation"]
    new = store.start_run(1, branch="branch", worktree=tmp_path / "worktree")
    assert new.started_at == first.started_at  # The timestamp alone cannot name this new execution.
    value = json.loads((tmp_path / "runs/1.json").read_text())
    assert value["run_generation"] != previous_generation
    assert "finished_at" not in value
    assert StateStore(tmp_path).get_run(1).status == "running"
    assert store.active_runs() == [new]
    finished = store.finish_run(1, "failed", "the next failure")
    assert StateStore(tmp_path).get_run(1) == finished
    assert store.read_history() == [finished]  # Public history retains its legacy timestamp deduplication.


def test_latest_finished_run_detaches_nested_history_values(tmp_path):
    store, _ = start(tmp_path)
    regions = {"wait": [NOW.isoformat(), NOW.isoformat()]}
    store.update_run(1, pause_regions=regions, changed_files=["original.py"])
    store.finish_run(1, "failed", INPUT_DETAIL)
    latest = store.get_run(1)
    latest.pause_regions["wait"].append("caller mutation")
    latest.changed_files.append("caller.py")
    peer = StateStore(tmp_path)
    for run in (store.get_run(1), peer.get_run(1), *store.read_history(), *peer.read_history()):
        assert run.pause_regions == regions
        assert run.changed_files == ["original.py"]


@pytest.mark.parametrize("side", [None, "dream", "draft"])
def test_detail_saved_while_running_keeps_the_complete_reason(tmp_path, side):
    store, _ = start(tmp_path)
    if side == "dream":
        store.start_dream("orch", worktree=tmp_path / "dream")
        key, read = "dream-orch", lambda: store.get_dream("orch")
    elif side == "draft":
        store.start_draft(2, "orch", worktree=tmp_path / "draft")
        key, read = "draft-2", lambda: store.get_draft(2)
    else:
        key, read = 1, lambda: store.get_run(1)
    store.update_run(key, detail=INPUT_DETAIL)
    assert read().detail == DETAIL
    store.finish_run(key, "failed")
    assert read().detail == DETAIL


@pytest.mark.parametrize("destination", ["runs.jsonl", "runs/1.json"])
def test_completed_metadata_updates_commit_through_history(tmp_path, monkeypatch, destination):
    store, run = start(tmp_path)
    initial = store.finish_run(1, "pr-open", "waiting for merge")
    peer = StateStore(tmp_path, now=lambda: NOW)
    # Warm both caches before another process writes a new completion fact.
    assert store.get_run(1) == peer.get_run(1) == initial
    fact = {"merged_at": (NOW + timedelta(minutes=1)).isoformat(), "head": "head"}
    original = store._atomic_write

    def fail(path, content):
        if path == store.root / destination:
            raise OSError("interrupted metadata write")
        return original(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_atomic_write", fail)
        with pytest.raises(OSError, match="interrupted metadata write"):
            store.record_owner_merge(1, run.started_at, fact)
    latest, = peer.read_history()
    assert peer.get_run(1) == store.get_run(1) == StateStore(tmp_path).get_run(1) == latest
    if destination == "runs.jsonl":
        assert latest == initial  # Failed writes cannot mutate a warmed read cache.
        assert peer.merge_times() == {}
    else:
        assert peer.merge_times() == {(1, run.started_at): fact["merged_at"]}
    store.record_owner_merge(1, run.started_at, fact)
    assert peer.merge_times() == {(1, run.started_at): fact["merged_at"]}
    before = (tmp_path / "runs.jsonl").read_bytes()
    store.record_owner_merge(1, run.started_at, fact)
    assert (tmp_path / "runs.jsonl").read_bytes() == before
    assert peer.get_run(1) == peer.read_history()[0]


def completed_record(root, legacy):
    store, run = start(root)
    store.update_run(1, last_review_pass_at=NOW.isoformat())
    store.finish_run(1, "pr-open", INPUT_DETAIL)
    value = json.loads((root / "runs/1.json").read_text())
    value.update(process_identities=[{"pid": 42000, "start_time": 10}], future_metadata={"keep": [1, 2]})
    if legacy:
        value.pop("run_generation")
    store._write_json(root / "runs/1.json", value)
    store._atomic_write(root / "runs.jsonl", json.dumps(value) + "\n")
    return store, run


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("action", ["owner", "ownership"])
@pytest.mark.parametrize("after_replace", [False, True])
def test_finished_metadata_crashes_keep_latest_and_history_equal(tmp_path, monkeypatch, legacy, action, after_replace):
    fact = {"merged_at": (NOW + timedelta(minutes=1)).isoformat(), "head": "head"}
    real_replace = os.replace
    for fail_at in range(1, 5):
        root = tmp_path / str(fail_at)
        store, run = completed_record(root, legacy)
        writes = []

        def update(store):
            if action == "owner":
                store.record_owner_merge(1, run.started_at, fact)
            elif store.get_run(1).process_identities:
                with store._state_lock():
                    store._write_run(replace(store.get_run(1), process_identities=[]))

        def interrupted(source, destination):
            writes.append(destination)
            if len(writes) == fail_at and not after_replace:
                raise OSError("before finished metadata write")
            real_replace(source, destination)
            if len(writes) == fail_at and after_replace:
                raise OSError("after finished metadata write")

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", interrupted)
            try:
                update(store)
            except OSError:
                pass
        reopened = StateStore(root, now=lambda: NOW)
        latest, = reopened.read_history()
        assert reopened.get_run(1) == latest
        assert json.loads((root / "runs.jsonl").read_text().splitlines()[-1])["future_metadata"] == {"keep": [1, 2]}
        assert latest.detail == DETAIL
        if action == "owner":
            assert latest.review_pass_to_merge_seconds in (None, 60.0)
        update(reopened)
        assert reopened.get_run(1) == reopened.read_history()[0]
        if action == "owner":
            assert reopened.get_run(1).review_pass_to_merge_seconds == 60.0
        else:
            assert reopened.get_run(1).process_identities == []


@pytest.mark.parametrize("legacy", [False, True])
def test_owner_readers_use_committed_fact_when_snapshot_is_stale(tmp_path, monkeypatch, legacy):
    store, run = completed_record(tmp_path, legacy)
    first = {"merged_at": (NOW + timedelta(minutes=1)).isoformat(), "head": "first"}
    second = {"merged_at": (NOW + timedelta(minutes=2)).isoformat(), "head": "second"}
    store.record_owner_merge(1, run.started_at, first)
    write = store._write_json

    def fail_snapshot(path, value):
        if path == tmp_path / "runs/1.json":
            raise OSError("snapshot failed")
        return write(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_write_json", fail_snapshot)
        with pytest.raises(OSError, match="snapshot failed"):
            store.record_owner_merge(1, run.started_at, second)
    reopened = StateStore(tmp_path)
    latest, = reopened.read_history()
    assert reopened.get_run(1) == latest
    assert latest.review_pass_to_merge_seconds == 120.0
    assert reopened.owner_merge_facts() == {(1, run.started_at): second}
    assert reopened.merge_times() == {(1, run.started_at): second["merged_at"]}


@pytest.mark.parametrize("legacy", [False, True])
def test_historical_owner_write_preserves_current_generation(tmp_path, monkeypatch, legacy):
    store, old = completed_record(tmp_path, legacy)
    store._now = lambda: NOW + timedelta(minutes=2)
    current = store.start_run(1, branch="new", worktree=tmp_path / "new")
    snapshot = tmp_path / "runs/1.json"
    before = snapshot.read_bytes()
    write = store._write_json

    def refuse_current(path, value):
        assert path != snapshot, "An old completion must not write the current snapshot"
        return write(path, value)

    fact = {"merged_at": (NOW + timedelta(minutes=1)).isoformat(), "head": "old"}
    monkeypatch.setattr(store, "_write_json", refuse_current)
    store.record_owner_merge(1, old.started_at, fact)
    reopened = StateStore(tmp_path)
    assert snapshot.read_bytes() == before
    assert reopened.get_run(1) == current
    recorded, = reopened.read_history()
    assert recorded.review_pass_to_merge_seconds == 60.0
    assert reopened.owner_merge_facts() == {(1, old.started_at): fact}
    history = (tmp_path / "runs.jsonl").read_bytes()
    reopened.record_owner_merge(1, old.started_at, fact)
    assert (tmp_path / "runs.jsonl").read_bytes() == history
    assert snapshot.read_bytes() == before


def test_legacy_owner_replay_commits_a_fact_previously_saved_only_in_snapshot(tmp_path):
    store, run = completed_record(tmp_path, True)
    fact = {"merged_at": (NOW + timedelta(minutes=1)).isoformat(), "head": "old-writer"}
    path = tmp_path / "runs/1.json"
    value = json.loads(path.read_text())
    value.update(owner_merge=fact, owner_merge_started_at=run.started_at, merged_at=fact["merged_at"],
                 review_pass_to_merge_seconds=60.0)
    store._write_json(path, value)  # An older writer crashed after its snapshot, before history.
    store.record_owner_merge(1, run.started_at, fact)
    reopened = StateStore(tmp_path)
    assert reopened.get_run(1) == reopened.read_history()[0]
    assert reopened.get_run(1).review_pass_to_merge_seconds == 60.0
    before = (tmp_path / "runs.jsonl").read_bytes()
    reopened.record_owner_merge(1, run.started_at, fact)
    assert (tmp_path / "runs.jsonl").read_bytes() == before
