"""The orchestrator watcher reports state transitions across invocations."""

from datetime import UTC, datetime
import json

import pytest

from foragent import orch
from foragent.config import Config
from foragent.git import GitRepo
from foragent.runner import RunContext
from foragent.state import StateStore
from foragent.tracker import FakeTracker


@pytest.fixture
def ctx(tmp_path):
    now = lambda: datetime(2026, 10, 21, tzinfo=UTC)
    return RunContext("owner/repo", tmp_path, FakeTracker(now=now),
                      StateStore(tmp_path / "state", now=now), Config(), GitRepo(tmp_path), now)


def test_label_removals_and_closed_items_change_the_watched_sets(ctx):
    ctx.tracker.add_issue(9, labels=["blocked", "needs-human"])
    ctx.tracker.add_issue(2, labels=["blocked"])
    ctx.tracker.add_issue(20, labels=["blocked"], is_pull_request=True)
    snapshots = [list(orch.watch(ctx))[0]]
    assert snapshots[0] == {"blocked": [2, 9], "needs_human": [9], "merged": [], "queue_empty": True}

    ctx.tracker.remove_labels(9, ["blocked", "needs-human"])
    snapshots += list(orch.watch(ctx))
    assert snapshots[-1]["blocked"] == [2]
    assert snapshots[-1]["needs_human"] == []
    ctx.tracker.close_issue(2)
    snapshots += list(orch.watch(ctx))
    assert snapshots[-1]["blocked"] == []
    assert json.loads((ctx.store.root / "orch-watch.json").read_text()) == snapshots[-1]
    assert all(not ctx.tracker.list_comments(n) for n in (2, 9, 20))
    assert not (ctx.store.root / "orch-events.jsonl").exists()


def test_queue_empty_tracks_refills_and_waits_for_active_run_completion(ctx, monkeypatch):
    assert list(orch.watch(ctx))[0]["queue_empty"] is True
    ctx.tracker.add_issue(1, labels=["approved"])
    assert list(orch.watch(ctx))[0]["queue_empty"] is False
    ctx.store.start_run(1, branch="foragent/issue-1", worktree="unused")
    ctx.tracker.close_issue(1)
    # The runner still has cleanup to perform after GitHub closes the issue.
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        assert len(polls) == 1, "Run completion must wake the next poll"
        ctx.store.finish_run(1, "merged")

    monkeypatch.setattr(orch.time, "sleep", sleep)
    assert list(orch.watch(ctx, interval=0.01))[0]["queue_empty"] is True
    assert polls == [0.01]
    ctx.tracker.add_issue(3, labels=["ready"])
    assert list(orch.watch(ctx))[0]["queue_empty"] is False


def test_unchanged_poll_order_and_closed_prs_do_not_wake_but_batch_merges_do(ctx, monkeypatch):
    ctx.tracker.add_issue(2, labels=["blocked"])
    ctx.tracker.add_issue(1, labels=["blocked"])
    assert list(orch.watch(ctx))[0]["blocked"] == [1, 2]
    list_issues = ctx.tracker.list_open_issues
    monkeypatch.setattr(ctx.tracker, "list_open_issues", lambda: list(reversed(list_issues())))

    closed = ctx.tracker.create_pr(head="foragent/issue-3", base="main", title="Closed", body="")
    ctx.tracker.close_pr(closed)
    unrelated = ctx.tracker.create_pr(head="feature", base="main", title="Unrelated", body="")
    ctx.tracker.merge_pr(unrelated, expected_head_sha=ctx.tracker.pr_head_sha(unrelated))
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        assert len(polls) == 1, "A merged batch must wake the next poll"
        pr = ctx.tracker.create_pr(head="foragent/batch-42", base="main", title="Batch", body="")
        ctx.tracker.merge_pr(pr, expected_head_sha=ctx.tracker.pr_head_sha(pr))

    monkeypatch.setattr(orch.time, "sleep", sleep)
    assert list(orch.watch(ctx, interval=0.01))[0]["merged"] == [42]
    assert polls == [0.01]


def test_repeated_failed_reads_print_once_in_follow_and_recover_with_the_real_snapshot(ctx, monkeypatch):
    ctx.tracker.add_issue(4, labels=["blocked"])
    known = list(orch.watch(ctx))[0]
    original = ctx.store.local_issues
    errors = ["disk busy", "disk busy"]

    def local_issues():
        if errors:
            raise OSError(errors.pop())
        return original()

    monkeypatch.setattr(ctx.store, "local_issues", local_issues)
    monkeypatch.setattr(orch.time, "sleep", lambda seconds: None)
    stream = orch.watch(ctx, interval=1, follow=True)
    assert next(stream) == {"unknown": True, "error": "disk busy"}
    # The second identical failure stays quiet; the recovered read prints although unchanged.
    assert next(stream) == known
    assert not (ctx.store.root / "orch-watch-unknown.json").exists()
    assert json.loads((ctx.store.root / "orch-watch.json").read_text()) == known


def test_follow_emits_inbox_records_written_while_the_first_reads_fail(ctx, monkeypatch):
    from foragent import orch_inbox

    old = orch_inbox.add_request(ctx.store, "planning", "before start")
    question = orch_inbox.ask(ctx.store, "Merge?", ["Merge"], 1, kind="merge")
    known = list(orch.watch(ctx))[0]
    original = ctx.store.local_issues
    failures = [1, 1]
    written = []

    def local_issues():
        if failures:
            failures.pop()
            if not written:  # written after watch started, before its first good read
                written.append(orch_inbox.add_request(ctx.store, "planning", "during failure"))
                orch_inbox.answer(ctx.store, question["id"], "Merge")
            raise OSError("disk busy")
        return original()

    monkeypatch.setattr(ctx.store, "local_issues", local_issues)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        assert len(sleeps) < 5, "the events never came; the loss would make this wait forever"

    monkeypatch.setattr(orch.time, "sleep", sleep)
    stream = orch.watch(ctx, interval=1, follow=True)
    assert next(stream)["unknown"] is True
    assert next(stream) == known
    assert next(stream) == {"event": "request", "id": written[0]["id"], "from": "planning",
                            "text": "during failure"}
    assert next(stream) == {"event": "answer", "id": question["id"], "kind": "merge", "answer": "Merge"}
    assert old["id"] != written[0]["id"]
