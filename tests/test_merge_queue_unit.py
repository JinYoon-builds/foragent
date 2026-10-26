from datetime import UTC, datetime

import pytest

from foragent.config import ConfigError, load_config
from foragent.state import StateStore


def store_with_runs(tmp_path, *numbers):
    store = StateStore(tmp_path / "state", now=lambda: datetime(2026, 10, 26, tzinfo=UTC))
    for number in numbers:
        store.start_run(number, branch=f"foragent/issue-{number}", worktree=tmp_path / str(number))
    return store


def entry(number, base="main", head="a" * 40):
    return {"number": number, "head_sha": head, "base": base}


def test_enqueue_keeps_fifo_position_and_updates_head(tmp_path):
    store = store_with_runs(tmp_path, 1, 2)
    store.enqueue_merge(entry(1))
    store.enqueue_merge(entry(2))
    store.enqueue_merge(entry(1, head="b" * 40))
    assert [(row["number"], row["head_sha"][0]) for row in store.merge_queue()] == [(1, "b"), (2, "a")]


def test_leader_reserves_its_slot_and_claims_oldest_unclaimed_peers(tmp_path):
    store = store_with_runs(tmp_path, 1, 2, 3, 4, 5)
    for number in (1, 2, 3, 4):
        store.enqueue_merge(entry(number))
    store.enqueue_merge(entry(5, base="other"))
    assert [row["number"] for row in store.claim_merge_group(4, "main", 2)] == [1, 4]
    # A second leader groups only unclaimed entries.
    assert [row["number"] for row in store.claim_merge_group(3, "main", 4)] == [2, 3]
    assert store.claim_merge_group(5, "other", 4) == []
    store.release_merge_claims(4)
    assert store.has_unclaimed_merge_peers(3, "main")  # 1 is free again; 2 stays with 3.
    assert {row["number"]: row["claimed_by"] for row in store.merge_queue()}[1] is None


def test_readers_drop_entries_and_claims_of_stopped_runs(tmp_path):
    store = store_with_runs(tmp_path, 1, 2, 3)
    for number in (1, 2, 3):
        store.enqueue_merge(entry(number))
    store.claim_merge_group(3, "main", 2)
    store.finish_run(3, "retries-exceeded")
    store.finish_run(2, "merged")
    assert store.claim_merge_group(1, "main", 4) == []
    assert [row["number"] for row in store.merge_queue()] == [1]
    store.dequeue_merge(1)
    assert store.merge_queue() == []


@pytest.mark.parametrize("value", [0, -1, "2", 1.5, True])
def test_merge_group_max_must_be_a_positive_integer(tmp_path, value):
    (tmp_path / ".foragent.yml").write_text(f"repo: o/r\nmerge_group_max: {value!r}\n".replace("'", '"'))
    with pytest.raises(ConfigError, match="merge_group_max"):
        load_config(tmp_path, global_path=tmp_path / "none.yml")


def test_merge_group_max_defaults_to_four_and_has_no_upper_limit(tmp_path):
    (tmp_path / ".foragent.yml").write_text("repo: o/r\n")
    assert load_config(tmp_path, global_path=tmp_path / "none.yml").merge_group_max == 4
    (tmp_path / ".foragent.yml").write_text("repo: o/r\nmerge_group_max: 1000\n")
    assert load_config(tmp_path, global_path=tmp_path / "none.yml").merge_group_max == 1000


def test_a_claimed_member_cannot_lead_another_group_before_release(tmp_path):
    store = store_with_runs(tmp_path, 1, 2, 3)
    for number in (1, 2, 3):
        store.enqueue_merge(entry(number))
    assert [row["number"] for row in store.claim_merge_group(3, "main", 2)] == [1, 3]
    # Member 1 belongs to leader 3's group; re-enqueueing keeps that claim.
    store.enqueue_merge(entry(1, head="c" * 40))
    assert store.claim_merge_group(1, "main", 4) == []
    assert not store.has_unclaimed_merge_peers(1, "main")
    assert {row["number"]: row["claimed_by"] for row in store.merge_queue()} == {1: 3, 2: None, 3: 3}
    store.release_merge_claims(1)  # Releasing never touches another leader's claims.
    assert {row["number"]: row["claimed_by"] for row in store.merge_queue()} == {1: 3, 2: None, 3: 3}


@pytest.mark.parametrize("checks,expected", [("local", True), ("ci", False)])
def test_grouping_follows_the_configured_checks_mode(checks, expected):
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.run_merge_group import grouping_enabled

    # checks: ci excludes groups even when no CI comparison is possible (npm test).
    config = Config(checks=checks, test_command="npm test")
    attempt = SimpleNamespace(ctx=SimpleNamespace(config=config), target_branch="main", batch_branch=None)
    assert grouping_enabled(attempt, True) is expected
    assert grouping_enabled(attempt, False) is False


def test_a_restarted_execution_never_inherits_its_old_entry_claim_or_marker(tmp_path):
    clock = [datetime(2026, 10, 26, tzinfo=UTC)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    for number in (1, 2):
        store.start_run(number, branch=f"foragent/issue-{number}", worktree=tmp_path / str(number))
        store.enqueue_merge(entry(number))
    store.claim_merge_group(2, "main", 2)
    store.record_group_merge(1, "a" * 40, store.get_run(1).started_at)
    assert store.group_merged_head(1) == "a" * 40
    # Both issues restart as new executions before either is ready again.
    from datetime import timedelta
    clock[0] += timedelta(minutes=5)
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "1")
    store.start_run(2, branch="foragent/issue-2", worktree=tmp_path / "2")
    assert store.group_merged_head(1) is None
    assert not store.has_unclaimed_merge_peers(2, "main")
    # The reader's cleanup is saved, not only computed.
    assert store.merge_queue() == []


def test_group_finish_keeps_local_facts_reinstall_and_journal_when_github_fails(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from foragent import runner
    from foragent.run_merge_group import finish_group
    from foragent.tracker import TrackerError

    store = store_with_runs(tmp_path, 1, 4, 5)
    reinstalls = []
    monkeypatch.setattr(runner, "after_merge", lambda ctx, pr, base: reinstalls.append(pr))

    class Tracker:
        def __init__(self):
            self.closed = []

        def current_issue(self, number):
            return SimpleNamespace(state="open")

        def close_issue(self, number, comment=None):
            if number == 1:
                raise TrackerError("HTTP 429: API rate limit exceeded")
            self.closed.append((number, comment))

        def remove_labels(self, number, labels):
            pass

    tracker = Tracker()
    monkeypatch.setattr("foragent.board.sync_status", lambda ctx, number, status: None)
    ctx = SimpleNamespace(store=store, tracker=tracker, now=store._now)
    attempt = SimpleNamespace(ctx=ctx, n=5, log=tmp_path / "5.log", target_branch="main")
    borrowed = [{"number": 1, "pr": 11, "head_sha": "1" * 40}, {"number": 4, "pr": 14, "head_sha": "4" * 40}]
    finish_group(attempt, borrowed, (15, "5" * 40), reinstall=True)
    for number in (1, 4, 5):
        assert store.get_run(number).merge_group_size == 3
        assert store.get_run(number).merge_group_bisect_checks == 0
    assert reinstalls == [15]
    assert tracker.closed == [(4, "Merged in a merge group with #1, #5.")]
    events = [json.loads(line) for line in (store.root / "orch-events.jsonl").read_text().splitlines()]
    assert [(row["issue"], row["kind"]) for row in events] == [(1, "merge"), (4, "merge"), (5, "merge")]
    assert "with #4, #5" in events[0]["summary"] and "PR #11" in events[0]["summary"]
    assert "finishing #1 failed" in (tmp_path / "5.log").read_text()
