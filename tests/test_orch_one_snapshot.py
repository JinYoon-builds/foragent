"""One ownership snapshot per tick or command, and no writes without one (#1004)."""

from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import orch_scope, runner, scheduler, scheduler_config, scheduler_stop  # noqa: E402
from foragent.config import Config  # noqa: E402
from foragent.run_attempt import OwnershipLost  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from foragent.tracker import FakeTracker, RecordingTracker, TrackerError  # noqa: E402
import test_orch_review_handoff as handoff  # noqa: E402
import test_stop_whole_tree as stop_fakes  # noqa: E402
from test_keep_unfinished_work import _assert_saved  # noqa: E402
from test_orch_stop import _case as stop_case  # noqa: E402

NOW = datetime(2026, 10, 31, tzinfo=UTC)


@pytest.fixture
def fake_processes(monkeypatch, tmp_path, no_forbidden_git_commands):
    return stop_fakes.fake_processes.__wrapped__(monkeypatch, tmp_path, no_forbidden_git_commands)


def queue_case(tmp_path, *issues, legacy="alice", committed=None):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    for number, labels in issues:
        tracker.add_issue(number, f"#{number}", "", labels=labels)
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config(legacy_owner=legacy))
    recorder = RecordingTracker(tracker, context, queue_writes=True)
    context.tracker = recorder
    store.refresh_local_issues([tracker.get_issue(n) for n, _ in issues], [], {}, NOW)
    if committed is not None:
        scheduler_config.record_tick_config(store, "origin/main:.foragent.yml", committed)
    return store, tracker, recorder, context


def events(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def relabel(tracker, number, old, new):
    tracker.remove_labels(number, [old])
    tracker.add_labels(number, [new])


def team_case(git_history, tmp_path, monkeypatch, cached, current):
    """Issue #1 cached as one owner while the tracker already shows another."""
    case = handoff.make_case(git_history, tmp_path, monkeypatch)
    object.__setattr__(case.ctx.config, "legacy_owner", "alice")
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, [f"by:{cached}"])
    case.ctx.tracker.get_issue(1)
    relabel(case.tracker, 1, f"by:{cached}", f"by:{current}")
    return case


@pytest.mark.parametrize("cached, current, restored", [("bob", "alice", True), ("alice", "bob", False)])
def test_stop_inner_paths_use_the_commands_entry_snapshot(
    git_history, tmp_path, monkeypatch, fake_processes, cached, current, restored,
):
    case, tree, main = stop_case(git_history, tmp_path, monkeypatch, fake_processes)
    object.__setattr__(case.ctx.config, "legacy_owner", "alice")
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.tracker.add_labels(1, [f"by:{cached}"])
    case.ctx.tracker.get_issue(1)
    relabel(case.tracker, 1, f"by:{cached}", f"by:{current}")
    code = main(["orch", "stop", "1", "--keep-work", "--why", "Keep the work"])
    labels = case.tracker.get_issue(1).labels
    if restored:
        assert code == 0 and not {row.pid for row in tree} & fake_processes.live
        assert "running" not in labels and "ready" in labels
        _assert_saved(case, "Keep the work")
    else:
        assert code == 1 and {row.pid for row in tree} <= fake_processes.live and "running" in labels


def test_a_tick_writes_nothing_to_a_team_issue_before_it_reads(git_history, tmp_path, monkeypatch):
    case = team_case(git_history, tmp_path, monkeypatch, "alice", "bob")
    run = SimpleNamespace(kind="issue", number=1, started_at=NOW.isoformat(), lane="main")
    seen = []

    def reap(ctx, report):
        seen.append(True)
        scheduler_stop._flag_stop_refused(ctx, run, report, PermissionError("EPERM"))

    monkeypatch.setattr(scheduler, "_reap_inactive_runs", reap)
    before = len(case.tracker.list_comments(1))
    scheduler.tick(handoff.tick_context(case))
    assert seen and len(case.tracker.list_comments(1)) == before
    assert "blocked" not in case.tracker.get_issue(1).labels


def test_a_failed_refresh_leaves_no_snapshot_to_write_from(git_history, tmp_path, monkeypatch):
    case = team_case(git_history, tmp_path, monkeypatch, "alice", "bob")
    ctx = handoff.tick_context(case)
    seen = []
    monkeypatch.setattr(case.tracker, "list_issues",
                        lambda state="all": (_ for _ in ()).throw(TrackerError("HTTP 502")))
    monkeypatch.setattr(scheduler, "_recover_runs",
                        lambda ctx, report: seen.append(orch_scope.may_write(ctx, 1)))
    scheduler.tick(ctx)
    assert seen == [False]


@pytest.mark.parametrize("effect", ["HTTP 502", "exit"])
def test_a_failed_run_entry_read_writes_nothing(git_history, tmp_path, monkeypatch, effect):
    case = team_case(git_history, tmp_path, monkeypatch, "alice", "bob")
    before = dict(comments=len(case.tracker.list_comments(1)), labels=case.tracker.get_issue(1).labels)

    def fails(_):
        if effect == "exit":
            raise SystemExit("stopped during the entry read")
        raise TrackerError(effect)

    monkeypatch.setattr(orch_scope, "observe", lambda tracker: SimpleNamespace(get_issue=fails))
    if effect == "exit":
        with pytest.raises(SystemExit):
            runner.run_issue(1, case.ctx)
    else:
        assert runner.run_issue(1, case.ctx) == "error"
    assert len(case.tracker.list_comments(1)) == before["comments"]
    assert case.tracker.get_issue(1).labels == before["labels"]


def test_foreign_run_issue_prints_one_line(git_history, tmp_path, monkeypatch, capsys):
    case = team_case(git_history, tmp_path, monkeypatch, "bob", "bob")
    assert runner.run_issue(1, case.ctx) == "refused"
    captured = capsys.readouterr()
    assert len(captured.err.splitlines()) == 1 and "bob" in captured.err and captured.out == ""


def test_ownership_lost_during_a_run_prints_its_one_line(git_history, tmp_path, monkeypatch, capsys):
    case = handoff.make_case(git_history, tmp_path, monkeypatch)
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    monkeypatch.setattr(runner, "kick_tick", lambda root: None)
    monkeypatch.setattr(runner, "_execute",
                        lambda n, ctx, worktree: (_ for _ in ()).throw(OwnershipLost("#1 is Bob's now")))
    assert runner.run_issue(1, case.ctx) == "refused"
    assert capsys.readouterr().err.splitlines() == ["#1 is Bob's now"]


@pytest.mark.parametrize("action, payload", [
    ("comment", {"body": "Mine once"}), ("add_labels", {"labels": ["ready"]}), ("close_issue", {"body": None})])
def test_an_observed_owner_edit_refuses_a_queued_write_inspectably(tmp_path, action, payload):
    store, tracker, recorder, _ = queue_case(tmp_path, (1, ["by:alice"]), (2, ["by:alice"]))
    store.queue_github_write(1, action, lanes={}, **payload)
    store.queue_github_write(2, "comment", body="Still mine")
    relabel(tracker, 1, "by:alice", "by:bob")
    recorder.get_issue(1)
    [refused] = store.refused_github_writes()
    assert refused["issue"] == 1 and refused["action"] == action and "by:bob" in refused["refused"]
    assert [entry["issue"] for entry in store.github_outbox()] == [2]
    assert [(e["kind"], e["issue"], e["action"], e["owner"]) for e in events(store)] == [
        ("ownership-refused", 1, action, "bob")]
    recorder.flush_outbox()
    assert tracker.list_comments(1) == [] and tracker.get_issue(1).state == "open"
    assert [c.body for c in tracker.list_comments(2)] == ["Still mine"]


def test_a_persons_other_label_edit_still_drops_the_write(tmp_path):
    store, tracker, recorder, _ = queue_case(tmp_path, (1, ["by:alice"]))
    store.queue_github_write(1, "add_labels", lanes={}, labels=["ready"])
    tracker.add_labels(1, ["blocked"])
    recorder.get_issue(1)
    assert store.github_outbox() == [] and store.refused_github_writes() == []


def test_a_pre_upgrade_foreign_entry_is_refused_and_lets_owned_writes_through(tmp_path):
    store, tracker, recorder, _ = queue_case(tmp_path, (1, ["by:bob"]), (2, ["by:alice"]))
    snapshot = json.loads((store.root / "issues.json").read_text())
    snapshot.setdefault("outbox", []).extend([  # The shape before intents had a premise or attempts.
        {"id": 1, "issue": 1, "action": "comment", "body": "Old foreign", "queued_at": NOW.isoformat()},
        {"id": 2, "issue": 2, "action": "comment", "body": "Old owned", "queued_at": NOW.isoformat()},
    ])
    snapshot["outbox_sequence"] = 2
    (store.root / "issues.json").write_text(json.dumps(snapshot))
    recorder.flush_outbox()
    assert tracker.list_comments(1) == []
    assert [c.body for c in tracker.list_comments(2)] == ["Old owned"]
    assert store.github_outbox() == [] and store.github_blocking_outbox() == []
    [refused] = store.refused_github_writes()
    assert refused["body"] == "Old foreign" and "bob" in refused["refused"]


def test_a_listing_keeps_a_queued_write_on_a_known_pr(tmp_path):
    store, tracker, recorder, _ = queue_case(tmp_path, (1, ["by:alice"]))
    store.update_local_issue(1, pr_number=50)
    store.queue_github_write(50, "comment", body="On the PR")
    store.refresh_local_issues([tracker.get_issue(1)], [], {}, NOW)
    recorder.list_issues()
    assert [entry["issue"] for entry in store.github_outbox()] == [50]


def test_a_known_issue_missing_from_a_listing_still_drops_its_writes(tmp_path):
    store, tracker, _, _ = queue_case(tmp_path, (1, ["by:alice"]), (2, ["by:alice"]))
    store.queue_github_write(2, "comment", body="Gone")
    store.refresh_local_issues([tracker.get_issue(1)], [], {}, NOW)
    assert store.github_outbox() == []


@pytest.mark.parametrize("content, legacy", [("broken: [\n", "alice"), ("legacy_owner: [\n", None)])
def test_an_invalid_team_config_authorizes_nothing(tmp_path, content, legacy):
    store, tracker, recorder, context = queue_case(tmp_path, (1, []), legacy=legacy, committed=content)
    assert orch_scope.may_write(context, 1) is False
    recorder.comment(1, "Refused")
    recorder.add_labels(1, ["blocked"])
    assert tracker.list_comments(1) == [] and "blocked" not in tracker.get_issue(1).labels


def test_a_broken_config_without_a_team_keeps_the_singleton(tmp_path):
    _, tracker, recorder, context = queue_case(tmp_path, (1, []), legacy=None, committed="broken: [\n")
    assert orch_scope.may_write(context, 1) is True
    recorder.comment(1, "Mine")
    assert [c.body for c in tracker.list_comments(1)] == ["Mine"]


def test_a_valid_committed_team_setting_decides_over_the_local_config(tmp_path):
    _, tracker, recorder, context = queue_case(tmp_path, (1, []), legacy=None, committed="legacy_owner: bob\n")
    assert context.config.legacy_owner is None
    assert orch_scope.may_write(context, 1) is False
    recorder.comment(1, "Refused")
    assert tracker.list_comments(1) == []
