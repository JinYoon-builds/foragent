"""Acceptance test for issue-record-cache. Locked by Foragent; do not edit."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from foragent.state import StateStore
from foragent.tracker import RecordingTracker


NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
PROJECT = "owner/17"
FIELD = "Status"


@pytest.fixture(autouse=True)
def isolated_offline_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key, directory in (
        ("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"), ("XDG_STATE_HOME", ".local/state"),
    ):
        monkeypatch.setenv(key, str(home / directory))

    def forbidden(*args, **kwargs):
        pytest.fail("Issue record cache tests must not start processes or use the network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    # tests/conftest.py also guards every real signal-delivery API.


@pytest.fixture
def store(tmp_path):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.root.mkdir()
    rows = {
        str(number): {
            "number": number, "title": "Initial title", "body": "Issue body",
            "labels": ["approved"], "lane": "main", "state": "open",
            "pr_number": None, "merged": False, "comments": [],
            "revision": 1, "updated_at": NOW.isoformat(),
            "board_values": {PROJECT: {FIELD: "Todo"}},
        }
        for number in (7, 8)
    }
    snapshot = {
        "refreshed_at": NOW.isoformat(), "revision": 1, "issues": rows,
        "outbox_sequence": 2, "outbox_base": deepcopy(rows),
        "outbox": [
            {"id": identity, "issue": number, "action": "comment",
             "body": "Pending comment", "queued_at": NOW.isoformat()}
            for identity, number in enumerate((7, 8), 1)
        ],
    }
    # Seed on disk without warming any implementation's in-memory cache.
    (store.root / "issues.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return store


@pytest.fixture
def issue_reads(store, monkeypatch):
    """Count only the issue-record reads, not unrelated JSON or lock files."""
    path = store.root / "issues.json"
    read_text = Path.read_text
    reads = []

    def counted_read_text(self, *args, **kwargs):
        if self == path:
            reads.append(self)
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read_text)
    return reads


def board_reader(store):
    # The real local lookup needs only a store; no remote tracker is available.
    return RecordingTracker(SimpleNamespace(), SimpleNamespace(store=store))


def test_issue_record_cache_reuses_one_record_for_200_local_and_board_lookups(store, issue_reads):
    tracker = board_reader(store)
    for index in range(200):
        assert [row["number"] for row in store.local_issues()] == [7, 8]
        assert tracker.board_value(7 + index % 2, PROJECT, FIELD) == "Todo"
        assert [entry["issue"] for entry in store.github_outbox()] == [7, 8]

    assert len(issue_reads) <= 1, (
        f"One unchanged issues.json version was read {len(issue_reads)} times; "
        "local_issues, board_value and github_outbox must reuse its parsed record"
    )


def test_issue_record_cache_invalidates_after_local_write_and_other_store_replace(store, issue_reads):
    path = store.root / "issues.json"
    peer = StateStore(store.root, now=lambda: NOW)
    tracker = board_reader(store)
    assert store.local_issues()[0]["title"] == "Initial title"
    assert peer.local_issues()[0]["title"] == "Initial title"

    store.update_local_issue(7, title="Updated title", board_values={PROJECT: {FIELD: "Done"}})
    before = len(issue_reads)
    assert store.local_issues()[0]["title"] == "Updated title"
    assert tracker.board_value(7, PROJECT, FIELD) == "Done"
    assert [entry["issue"] for entry in store.github_outbox()] == [7, 8]
    local_reads = len(issue_reads) - before
    assert peer.local_issues()[0]["title"] == "Updated title", "An existing peer must see the write"

    replacement = json.loads(path.read_text(encoding="utf-8"))
    replacement["issues"]["7"]["title"] = "Outside title"
    replacement["issues"]["7"]["board_values"][PROJECT][FIELD] = "Wait"
    replacement["outbox"][0]["body"] = "Changed comment"
    replacement["revision"] += 1
    previous = path.stat()
    with peer._state_lock():
        peer._write_json(path, replacement)
        # Equal size and timestamp must not hide a different file identity.
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    current = path.stat()
    assert current.st_ino != previous.st_ino, "The peer must actually replace the file"
    assert (current.st_size, current.st_mtime_ns) == (previous.st_size, previous.st_mtime_ns)

    before = len(issue_reads)
    assert store.local_issues()[0]["title"] == "Outside title"
    assert store.github_outbox()[0]["body"] == "Changed comment"
    assert tracker.board_value(7, PROJECT, FIELD) == "Wait"
    external_reads = len(issue_reads) - before
    reopened = StateStore(store.root, now=lambda: NOW)
    assert reopened.local_issues()[0]["title"] == "Outside title"
    assert local_reads <= 1 and external_reads <= 1, (
        "Each new file version must be parsed at most once by the next group of reads; "
        f"got local-write={local_reads}, peer-replace={external_reads}"
    )


def test_issue_record_cache_keeps_returned_lists_and_nested_rows_detached(store, issue_reads):
    expected = deepcopy(store.local_issues())
    returned = store.local_issues()
    returned[0]["title"] = "Caller changed this"
    returned[0]["labels"].append("caller-only")
    returned[0]["board_values"][PROJECT][FIELD] = "Caller value"
    returned.pop()
    returned.append({"number": 999, "title": "Caller-only row"})

    assert store.local_issues() == expected, "Returned lists and nested rows must not mutate the cache"
    assert board_reader(store).board_value(7, PROJECT, FIELD) == "Todo"
    assert len(issue_reads) <= 1, (
        "Caller isolation must preserve the parsed cache rather than reread the unchanged file; "
        f"got {len(issue_reads)} reads"
    )


def test_issue_record_cache_drop_under_lock_preserves_a_peers_latest_write(store, issue_reads, monkeypatch):
    assert [row["number"] for row in store.local_issues()] == [7, 8]
    assert [entry["issue"] for entry in store.github_outbox()] == [7, 8]
    peer = StateStore(store.root, now=lambda: NOW)
    state_lock = store._state_lock

    @contextmanager
    def lock_after_peer_write():
        # Deterministically model a peer finishing just before this lock is acquired.
        peer.update_local_issue(8, title="Peer's latest title", labels=["approved", "peer-label"])
        peer.queue_github_write(8, "comment", body="Peer's pending comment")
        with state_lock():
            yield

    monkeypatch.setattr(store, "_state_lock", lock_after_peer_write)
    store.drop_missing_github_issue(7)

    before = len(issue_reads)
    rows = store.local_issues()
    pending = store.github_outbox()
    assert store.local_issues() == rows
    next_reads = len(issue_reads) - before
    assert [row["number"] for row in rows] == [8]
    assert rows[0]["title"] == "Peer's latest title"
    assert rows[0]["labels"] == ["approved", "peer-label"]
    assert rows[0]["comments"][-1]["body"] == "Peer's pending comment"
    assert [(entry["id"], entry["issue"], entry["body"]) for entry in pending] == [
        (2, 8, "Pending comment"), (3, 8, "Peer's pending comment"),
    ], "Dropping issue 7 must preserve the peer's pending writes for issue 8"
    saved = json.loads((store.root / "issues.json").read_text(encoding="utf-8"))
    assert set(saved["outbox_base"]) == {"8"}
    assert saved["revision"] == 4, "The drop must increment the latest persisted revision"
    assert next_reads <= 1, (
        "The drop must publish one new record version reusable by all subsequent readers; "
        f"got {next_reads} reads"
    )
