from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import cli
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import Dependency, FakeTracker, RecordingTracker


def local_status(tracker, store, config=None):
    """Status reads only local records, so first record what the gate would observe."""
    config = config or Config()
    context = SimpleNamespace(store=store, config=config, now=lambda: datetime.now(UTC))
    RecordingTracker(tracker, context).refresh_issues()
    return cli._status(tracker, store, config)


NOW = datetime(2026, 10, 21, 9, tzinfo=UTC)


def test_same_lane_keeps_active_issue_and_counts_all_queued_work(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(lanes={"web": {"title": "화면"}, "docs": {}})
    tracker.add_issue(1, "Next", labels=["approved", "ready", "lane:web"])
    tracker.add_issue(2, "Later", labels=["approved", "lane:web"])
    tracker.add_issue(3, "Active", labels=["approved", "running", "lane:web"])
    store.start_run(3, branch="branch", worktree="wt", lane="web")
    status = local_status(tracker, store, config)
    assert status["summary"] == dict(working=1, blocked=0, waiting_human=0, queued=2)
    assert status["lanes"]["web"]["issue"] == 3
    assert status["lanes"]["web"]["state"] == "working"
    assert status["lanes"]["web"]["ready"] == [{"number": 1, "title": "Next"}]
    assert status["lanes"]["docs"]["state"] == "idle"


def test_queue_only_waits_for_open_dependencies_and_ignores_old_execution(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    tracker.add_issue(1, "Retry", labels=["approved", "ready"])
    tracker.set_blocked_by(1, [Dependency(5, "closed"), Dependency(3, "open"), Dependency(2, "open")])
    store.start_run(1, branch="branch", worktree="wt")
    store.update_run(1, phase="implementing", phase_started_at=(NOW - timedelta(seconds=10)).isoformat())
    store.finish_run(1, "limit-wait")
    row = local_status(tracker, store)["lanes"]["main"]
    assert row["state"] == "queued" and row["waiting_for"] == [2, 3]
    assert row["phase"] is None and row["elapsed_seconds"] is None


def test_blockers_override_running_labels_and_unapproved_work_stays_idle(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    config = Config(lanes={"api": {}, "web": {}, "docs": {}})
    tracker.add_issue(1, labels=["approved", "blocked", "running", "lane:api"])
    tracker.add_issue(2, labels=["approved", "blocked", "needs-human", "running", "lane:web"])
    tracker.add_issue(3, labels=["lane:docs"])
    for number, slug in ((1, "api"), (2, "web")):
        store.start_run(number, branch="branch", worktree="wt", lane=slug)
    status = local_status(tracker, store, config)
    assert status["summary"] == dict(working=0, blocked=1, waiting_human=1, queued=0)
    assert status["lanes"]["api"]["state"] == "blocked"
    assert status["lanes"]["web"]["state"] == "waiting-human"
    assert status["lanes"]["docs"]["state"] == "idle"


def test_open_pr_waits_for_human_and_language_variants_translate(tmp_path, capsys):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    tracker.add_issue(1, "Review this", labels=["approved"])
    tracker.create_pr(head="foragent/issue-1", base="main", title="Review", body="")
    status = local_status(tracker, store, Config(language="ko-KR"))
    assert status["lanes"]["main"]["state"] == "waiting-human"
    cli._print_status(status | {"launchd": {"loaded": False}}, "ko-KR")
    assert capsys.readouterr().out.startswith("일하는 중 0 · 확인할 일 1 · 기다리는 일 0\n")


@pytest.mark.parametrize(("labels", "expected_state"), [
    (["approved"], "waiting-human"),
    (["approved", "blocked"], "blocked"),
    (["approved", "needs-human"], "waiting-human"),
    (["approved", "blocked", "running"], "blocked"),
    (["approved", "needs-human", "running"], "waiting-human"),
], ids=["pr-open", "blocked", "needs-human", "blocked-running", "needs-human-running"])
def test_finished_run_does_not_show_stale_phase_or_ages(tmp_path, capsys, labels, expected_state):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    tracker.add_issue(1, "Review this", labels=labels)
    tracker.create_pr(head="foragent/issue-1", base="main", title="Review", body="")
    store.start_run(1, branch="foragent/issue-1", worktree="wt")
    store.update_run(
        1, phase="reviewing",
        phase_started_at=(NOW - timedelta(seconds=120)).isoformat(),
        last_activity_at=(NOW - timedelta(seconds=660)).isoformat(),
    )
    active_row = local_status(tracker, store)["lanes"]["main"]
    assert active_row["phase"] == "reviewing"
    assert active_row["elapsed_seconds"] is not None
    assert active_row["last_activity_seconds"] is not None

    store.finish_run(1, "pr-open")
    status = local_status(tracker, store)
    row = status["lanes"]["main"]
    assert row["state"] == expected_state
    assert {key: row[key] for key in ("phase", "elapsed_seconds", "last_activity_seconds")} == {
        "phase": None, "elapsed_seconds": None, "last_activity_seconds": None,
    }
    assert store.get_run(1).phase == "reviewing"

    cli._print_status(status | {"launchd": {"loaded": False}})
    output = capsys.readouterr().out
    assert "reviewing" not in output
    assert "elapsed" not in output
    assert "last activity" not in output


def test_new_open_pr_wins_over_an_older_merged_pr_for_the_same_issue(tmp_path):
    tracker = FakeTracker()
    store = StateStore(tmp_path, now=lambda: NOW)
    tracker.add_issue(1, "Reopened work", labels=["approved"])
    old = tracker.create_pr(head="foragent/issue-1", base="main", title="First", body="Closes #1")
    tracker.merge_pr(old, expected_head_sha=tracker.pr_head_sha(old))
    tracker.issues[1] = tracker.issues[1].__class__(1, "Reopened work", "", frozenset({"approved"}), "open")
    new = tracker.create_pr(head="foragent/issue-1", base="main", title="Second", body="Closes #1")
    status = local_status(tracker, store)
    assert [item["number"] for item in status["issues"]["pr-open"]] == [1]
    assert status["summary"]["queued"] == 0 and status["summary"]["waiting_human"] == 1
    row = next(row for row in store.local_issues() if row["number"] == 1)
    assert row["pr_number"] == new and row["merged"] is False
