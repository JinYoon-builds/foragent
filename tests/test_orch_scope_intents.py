"""The person boundary on the GitHub intent log: observed owner edits and known PR targets (#1004)."""

import json
from datetime import UTC, datetime
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent.config import Config  # noqa: E402
from foragent.state import StateStore  # noqa: E402
from foragent.tracker import FakeTracker, RecordingTracker  # noqa: E402

NOW = datetime(2026, 10, 31, tzinfo=UTC)


def queue_case(tmp_path, *issues):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    for number, labels in issues:
        tracker.add_issue(number, f"#{number}", "", labels=labels)
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config(legacy_owner="alice"))
    recorder = RecordingTracker(tracker, context, queue_writes=True)
    context.tracker = recorder
    store.refresh_local_issues([tracker.get_issue(n) for n, _ in issues], [], {}, NOW)
    return store, tracker, recorder


def events(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def visible(store, number):
    return next(row for row in store.local_issues() if row["number"] == number)


def relabel(tracker, number, old, new):
    tracker.remove_labels(number, [old])
    tracker.add_labels(number, [new])


@pytest.mark.parametrize("action, payload", [
    ("comment", {"body": "Mine once"}), ("add_labels", {"labels": ["ready"]}), ("close_issue", {"body": None})])
@pytest.mark.parametrize("read", ["bulk", "single"])
def test_an_observed_owner_edit_refuses_the_write_inspectably(tmp_path, action, payload, read):
    store, tracker, recorder = queue_case(tmp_path, (1, ["by:alice"]), (2, ["by:alice"]))
    store.queue_github_write(1, action, lanes={}, **payload)
    store.queue_github_write(2, "comment", body="Still mine")
    relabel(tracker, 1, "by:alice", "by:bob")

    if read == "bulk":
        store.refresh_local_issues([tracker.get_issue(1), tracker.get_issue(2)], [], {}, NOW)
    else:
        recorder.get_issue(1)

    [refused] = store.refused_github_writes()
    assert refused["issue"] == 1 and refused["action"] == action
    assert "by:alice" in refused["refused"] and "by:bob" in refused["refused"]
    assert [entry["issue"] for entry in store.github_outbox()] == [2]
    assert visible(store, 1)["labels"] == ["by:bob"] and visible(store, 1)["state"] == "open"
    assert [(e["kind"], e["issue"], e["action"], e["owner"]) for e in events(store)] == [
        ("ownership-refused", 1, action, "bob")]

    recorder.flush_outbox()
    assert tracker.list_comments(1) == [] and tracker.get_issue(1).state == "open"
    assert [c.body for c in tracker.list_comments(2)] == ["Still mine"]


def test_a_persons_other_label_edit_still_drops_the_write(tmp_path):
    store, tracker, recorder = queue_case(tmp_path, (1, ["by:alice"]))
    store.queue_github_write(1, "comment", body="Stale")
    tracker.add_labels(1, ["blocked"])

    recorder.get_issue(1)

    assert store.github_outbox() == [] and store.refused_github_writes() == []
    assert [e["kind"] for e in events(store)] == ["outbox-dropped"]


def test_refusal_keeps_a_sent_write_and_removes_only_the_unsent_effect(tmp_path):
    store, _, _ = queue_case(tmp_path, (1, ["by:alice"]))
    sent = store.queue_github_write(1, "add_labels", lanes={}, labels=["sent"])
    store.record_github_attempt(sent["id"], NOW, True)
    store.queue_github_write(1, "add_labels", lanes={}, labels=["unsent"])
    assert visible(store, 1)["labels"] == ["by:alice", "sent", "unsent"]

    assert store.refuse_github_writes(1, "not yours") == 1

    assert [entry["labels"] for entry in store.refused_github_writes()] == [["unsent"]]
    assert visible(store, 1)["labels"] == ["by:alice", "sent"]  # Awaits the read that shows it.
    assert store.github_write_issues() == {1}


@pytest.mark.parametrize("read", ["listing", "bulk"])
def test_a_complete_listing_does_not_call_a_known_pr_missing(tmp_path, read):
    store, tracker, recorder = queue_case(tmp_path, (1, ["by:alice"]))
    store.update_local_issue(1, pr_number=7)
    store.queue_github_write(7, "comment", body="On the PR")

    if read == "listing":
        recorder.list_issues("all")
    else:
        store.refresh_local_issues([tracker.get_issue(1)], [], {}, NOW)

    assert [entry["issue"] for entry in store.github_outbox()] == [7]
    assert [e["kind"] for e in events(store)] == []


@pytest.mark.parametrize("entry", ["run_issue", "review_only"])
def test_a_refused_entry_takes_no_machine_slot(git_history, tmp_path, monkeypatch, entry):
    from foragent import capacity, runner
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    object.__setattr__(case.ctx.config, "legacy_owner", "alice")
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.tracker.add_labels(1, ["by:bob"])
    taken = []
    monkeypatch.setattr(capacity, "hold_workload", lambda *a, **k: taken.append(k) or (_ for _ in ()).throw(
        AssertionError("a refused entry must not wait for a machine slot")))

    assert getattr(runner, entry)(1, case.ctx) == "refused"
    assert taken == []


def test_evidence_outlives_an_owner_edit_until_replay_refuses_it(tmp_path):
    store, tracker, recorder = queue_case(tmp_path, (1, ["by:alice"]))
    store.queue_github_write(1, "comment", body="Accepted advice", evidence=True)
    relabel(tracker, 1, "by:alice", "by:bob")

    recorder.get_issue(1)
    assert [entry["body"] for entry in store.github_outbox()] == ["Accepted advice"]

    recorder.flush_outbox()
    assert store.github_outbox() == [] and tracker.list_comments(1) == []
    assert [entry["body"] for entry in store.refused_github_writes()] == ["Accepted advice"]


def team_run_case(git_history, tmp_path, monkeypatch, stored, actual):
    """A run whose stored row says ``stored`` while GitHub says ``actual``; no team setting."""
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.tracker.add_labels(1, stored)
    case.ctx.store.record_issue(case.tracker.get_issue(1), {})
    for label in stored:
        case.tracker.remove_labels(1, [label])
    case.tracker.add_labels(1, actual)
    return case


def shared_effects(case):
    return (sorted(case.tracker.get_issue(1).labels), [c.body for c in case.tracker.list_comments(1)])


@pytest.mark.parametrize("effect", ["HTTP 502", "exit"])
def test_a_failed_entry_read_never_lets_a_stale_row_authorize_effects(git_history, tmp_path, monkeypatch, effect):
    from foragent import orch_scope, runner
    from foragent.tracker import TrackerError

    case = team_run_case(git_history, tmp_path, monkeypatch, ["by:alice"], ["by:bob"])
    before = shared_effects(case)

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
    assert shared_effects(case) == before


@pytest.mark.parametrize("effect", ["HTTP 502", "exit"])
def test_a_singleton_row_still_reports_a_failed_entry_read(git_history, tmp_path, monkeypatch, effect):
    from foragent import orch_scope, runner
    from foragent.tracker import TrackerError

    case = team_run_case(git_history, tmp_path, monkeypatch, [], [])

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
    assert "blocked" in case.tracker.get_issue(1).labels


@pytest.mark.parametrize("labels, owned", [(["by:bob"], False), (["by:alice"], True), ([], True)])
def test_a_target_without_a_row_is_read_once_before_any_write(tmp_path, labels, owned):
    from foragent import orch_scope

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Work", "", labels=labels)
    reads = []
    read = tracker.get_issue
    tracker.get_issue = lambda n: reads.append(n) or read(n)
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config())
    recorder = RecordingTracker(tracker, context)
    context.tracker = recorder

    assert orch_scope.may_write(context, 1) is owned
    assert orch_scope.may_write(context, 1) is owned
    assert reads == [1]
    tracker.get_issue = read  # FakeTracker's own writes use this method; only the decision is counted.
    recorder.comment(1, "Status")
    recorder.add_labels(1, ["blocked"])

    assert [c.body for c in tracker.list_comments(1)] == (["Status"] if owned else [])
    assert ("blocked" in read(1).labels) is owned


def test_an_unavailable_read_refuses_the_cleanup_and_decides_nothing(tmp_path):
    from foragent import orch_scope
    from foragent.tracker import TrackerError

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Work", "", labels=["by:alice"])
    read, down = tracker.get_issue, [True]

    def unavailable(n):
        if down:
            raise TrackerError("HTTP 502")
        return read(n)

    tracker.get_issue = unavailable
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config())
    context.tracker = RecordingTracker(tracker, context)

    assert orch_scope.may_write(context, 1) is False  # Recovery keeps its local work; no flag, no push.
    down.clear()
    assert orch_scope.may_write(context, 1) is True  # A failed read was not an ownership decision.


def test_a_github_wait_queues_an_unknown_target_and_replay_judges_it(tmp_path):
    from datetime import timedelta

    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Bob's", "", labels=["by:bob"])
    context = SimpleNamespace(store=store, now=lambda: clock[0], config=Config())
    recorder = RecordingTracker(tracker, context, queue_writes=True)
    context.tracker = recorder
    store.pause_github(NOW + timedelta(minutes=1))

    recorder.comment(1, "Queued")
    assert [entry["body"] for entry in store.github_outbox()] == ["Queued"]

    clock[0] = NOW + timedelta(minutes=2)
    recorder.flush_outbox()
    assert tracker.list_comments(1) == [] and store.github_outbox() == []
    assert [entry["body"] for entry in store.refused_github_writes()] == ["Queued"]


@pytest.mark.parametrize("owner, sent", [("bob", False), ("alice", True)])
def test_a_queued_write_on_an_older_pr_is_judged_by_that_prs_issue(tmp_path, owner, sent):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Work", "", labels=[f"by:{owner}"])
    older = tracker.create_pr(head="foragent/issue-1", base="main", title="First", body="")
    newer = tracker.create_pr(head="foragent/issue-1", base="main", title="Second", body="")
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config())
    recorder = RecordingTracker(tracker, context, queue_writes=True)
    context.tracker = recorder
    store.refresh_local_issues([tracker.get_issue(1)], [], {}, NOW)
    store.update_local_issue(1, pr_number=newer)  # A row keeps only the newest PR.
    store.queue_github_write(older, "comment", body="On the older PR")

    recorder.flush_outbox()

    assert ([c.body for c in tracker.list_comments(older)] == ["On the older PR"]) is sent
    assert store.github_outbox() == []
    assert bool(store.refused_github_writes()) is not sent


def test_a_new_tick_never_reuses_the_last_ticks_decisions_and_reads_identity_once(
    git_history, tmp_path, monkeypatch,
):
    from foragent import orch_scope, scheduler
    from test_orch_review_handoff import make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch)
    object.__setattr__(case.ctx.config, "legacy_owner", "alice")
    logins = []
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: logins.append(1) or "alice", raising=False)
    case.tracker.add_labels(1, ["by:alice"])
    ctx = tick_context(case, start_runs=False)
    ctx.store.record_issue(case.tracker.get_issue(1), {})
    assert orch_scope.may_write(ctx, 1) is True  # The earlier tick's decision, for Alice.
    logins.clear()
    case.tracker.remove_labels(1, ["by:alice"])
    case.tracker.add_labels(1, ["by:bob"])
    ctx.store.record_issue(case.tracker.get_issue(1), {})
    decided = []
    monkeypatch.setattr(scheduler, "_reap_inactive_runs",
                        lambda tick_ctx, report: decided.append(orch_scope.may_write(tick_ctx, 1)))

    scheduler.tick(ctx)

    assert decided == [False]
    assert len(logins) == 1


def config_case(tmp_path, content, *, local=None, unreadable=False):
    from foragent.scheduler_config import record_tick_config

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Unlabelled", "")
    tracker.add_issue(2, "Bob's", "", labels=["by:bob"])
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config(legacy_owner=local))
    recorder = RecordingTracker(tracker, context, queue_writes=True)
    context.tracker = recorder
    store.refresh_local_issues([tracker.get_issue(1), tracker.get_issue(2)], [], {}, NOW)
    record_tick_config(store, "origin/main:.foragent.yml", content, unreadable=unreadable)
    return store, tracker, recorder, context


@pytest.mark.parametrize("content, local, unreadable", [
    ("legacy_owner: 'not a login!'\n", None, False),  # A team setting that does not validate.
    ("legacy_owner: alice\nbroken: [\n", None, False),  # A file naming one that does not parse.
    ("broken: [\n", "alice", False),  # A team factory whose committed file does not parse.
    (None, "alice", True),  # A team factory whose committed file cannot be read.
    (None, None, True),  # Any factory: an unreadable origin file may hold a team setting.
    ("\"legacy_owner\": 'not a login!'\n", None, False),  # Quoted key.
    ("{legacy_owner: 'not a login!'}\n", None, False),  # Flow mapping.
    ("? legacy_owner\n: 'not a login!'\n", None, False),  # Explicit key.
])
def test_an_unreadable_team_setting_authorizes_nothing_and_keeps_queued_writes(tmp_path, content, local, unreadable):
    from foragent import orch_scope

    store, tracker, recorder, context = config_case(tmp_path, content, local=local, unreadable=unreadable)
    store.queue_github_write(1, "comment", body="Waits for a valid setting")

    assert orch_scope.may_write(context, 1) is False
    recorder.comment(1, "Refused")
    recorder.add_labels(1, ["blocked"])
    recorder.flush_outbox()

    assert tracker.list_comments(1) == [] and "blocked" not in tracker.get_issue(1).labels
    assert [entry["body"] for entry in store.github_outbox()] == ["Waits for a valid setting"]
    assert store.refused_github_writes() == []
    assert orch_scope.check(SimpleNamespace(store=store, config=context.config, tracker=recorder), 1) is not None


@pytest.mark.parametrize("content", ["broken: [\n", None])
def test_a_singleton_repo_keeps_its_local_config_when_origin_has_no_team_setting(tmp_path, content):
    from foragent import orch_scope

    _, tracker, recorder, context = config_case(tmp_path, content)
    assert orch_scope.may_write(context, 1) is True
    assert orch_scope.may_write(context, 2) is False  # An owner label still counts.
    recorder.comment(1, "Mine")
    assert [c.body for c in tracker.list_comments(1)] == ["Mine"]


def test_a_valid_committed_team_setting_overrides_a_stale_local_config(tmp_path):
    from foragent import orch_scope

    _, tracker, recorder, context = config_case(tmp_path, "legacy_owner: bob\n")
    assert context.config.legacy_owner is None
    assert orch_scope.may_write(context, 1) is False  # Unlabelled work is Bob's, whatever this checkout says.
    recorder.comment(1, "Refused")
    assert tracker.list_comments(1) == []


def test_a_pre_upgrade_foreign_entry_is_refused_and_lets_owned_writes_through(tmp_path):
    store, tracker, recorder = queue_case(tmp_path, (1, ["by:bob"]), (2, ["by:alice"]))
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
    assert [(e["kind"], e["owner"]) for e in events(store)] == [("ownership-refused", "bob")]


def test_a_foreign_running_label_never_reaches_a_signal(git_history, tmp_path, monkeypatch):
    from foragent import scheduler
    from test_orch_review_handoff import make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch)
    object.__setattr__(case.ctx.config, "legacy_owner", "alice")
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.tracker.add_issue(9, "Bob's running work", "", labels=["by:bob", "running"])
    killed = []
    ctx = tick_context(case, start_runs=False)
    ctx.kill_runner = killed.append

    scheduler.tick(ctx)

    assert killed == []
    assert sorted(case.tracker.get_issue(9).labels) == ["by:bob", "running"]
    assert ctx.store.get_run(9) is None


def test_a_foreign_blocker_holds_owned_work_until_it_really_closes(git_history, tmp_path, monkeypatch):
    from foragent import scheduler
    from foragent.tracker import Issue
    from test_orch_review_handoff import make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch)
    object.__setattr__(case.ctx.config, "legacy_owner", "alice")
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["by:alice"])
    case.tracker.add_issue(7, "Bob's", "", labels=["by:bob"])
    state = ["open"]
    monkeypatch.setattr(scheduler, "_blockers", lambda ctx, n, confirmed=False: [
        Issue(7, "Bob's", "", frozenset({"by:bob"}), state[0], state_reason="completed" if state[0] == "closed" else None)])
    ctx = tick_context(case)
    issue, approval = case.tracker.get_issue(1), case.store.load_approval(1)

    assert scheduler._check_prerequisites(ctx, issue, approval, scheduler.TickReport()) is False
    state[0] = "closed"
    assert scheduler._check_prerequisites(ctx, issue, approval, scheduler.TickReport()) is True
    assert sorted(case.tracker.get_issue(7).labels) == ["by:bob"] and case.tracker.list_comments(7) == []


@pytest.mark.parametrize("labels, owned", [(["by:bob"], False), (["by:alice"], True)])
def test_an_issue_observed_after_the_snapshot_is_judged_without_another_read(tmp_path, labels, owned):
    from foragent import orch_scope

    store, tracker, recorder = queue_case(tmp_path, (1, ["by:alice"]))
    context = recorder._context
    assert orch_scope.may_write(context, 1) is True  # The snapshot is taken here, without #2.
    issue = tracker.add_issue(2, "Later", "", labels=labels)
    store.record_issue(issue, {})
    reads = []
    tracker.get_issue = lambda n: reads.append(n) or issue

    assert orch_scope.may_write(context, 2) is owned
    assert reads == []


@pytest.mark.parametrize("record", ["{not json", '{"source": "x"}', "[]"])
def test_a_broken_config_record_authorizes_nothing(tmp_path, record):
    from foragent import orch_scope
    from foragent.scheduler_config import TICK_CONFIG_FILE

    store, tracker, _, context = config_case(tmp_path, "max_parallel: 1\n")
    (store.root / TICK_CONFIG_FILE).write_text(record, encoding="utf-8")
    context._scope = None
    assert orch_scope.may_write(context, 1) is False


@pytest.mark.parametrize("read", ["bulk", "listing"])
@pytest.mark.parametrize("state", ["open", "closed"])
def test_a_listing_keeps_a_queued_write_on_an_older_pr(tmp_path, read, state):
    """Neither listing shows it; only the replay can say what the number is."""
    store, tracker, recorder = queue_case(tmp_path, (1, ["by:bob"]))
    older = tracker.create_pr(head="foragent/issue-1", base="main", title="Old", body="")
    newer = tracker.create_pr(head="foragent/issue-1", base="main", title="New", body="")
    if state == "closed":
        tracker.close_pr(older)
    store.update_local_issue(1, pr_number=newer)
    store.queue_github_write(older, "comment", body="Keep me")

    if read == "listing":
        recorder.list_issues("all")
    else:
        store.refresh_local_issues([tracker.get_issue(1)], [], {}, NOW)

    assert [entry["body"] for entry in store.github_outbox()] == ["Keep me"]
    assert [e["kind"] for e in events(store)] == []

    recorder.flush_outbox()
    assert store.github_outbox() == [] and tracker.list_comments(older) == []
    assert [entry["body"] for entry in store.refused_github_writes()] == ["Keep me"]


def test_a_known_issue_missing_from_a_listing_still_drops_its_writes(tmp_path):
    store, tracker, _ = queue_case(tmp_path, (1, ["by:alice"]), (2, ["by:alice"]))
    store.queue_github_write(2, "comment", body="Gone with the issue")

    store.refresh_local_issues([tracker.get_issue(1)], [], {}, NOW)

    assert store.github_outbox() == []
    assert store.refused_github_writes() == []


def test_a_write_queued_for_an_unknown_number_truly_gone_is_dropped_at_replay(tmp_path):
    store, tracker, recorder = queue_case(tmp_path, (1, ["by:alice"]))
    store.queue_github_write(99, "comment", body="Nobody")

    recorder.flush_outbox()  # Scoped as absent: the send's own 404 retires it.

    assert store.github_outbox() == []


def test_foreign_run_issue_prints_one_line(git_history, tmp_path, monkeypatch, capsys):
    from foragent.runner import run_issue
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    object.__setattr__(case.ctx.config, "legacy_owner", "alice")
    monkeypatch.setattr(case.tracker, "authenticated_login", lambda **kw: "alice", raising=False)
    case.tracker.add_labels(1, ["by:bob"])

    assert run_issue(1, case.ctx) == "refused"

    captured = capsys.readouterr()
    assert len(captured.err.splitlines()) == 1 and "bob" in captured.err and captured.out == ""


def test_ownership_lost_during_a_run_prints_its_one_line(git_history, tmp_path, monkeypatch, capsys):
    from foragent import runner
    from foragent.run_attempt import OwnershipLost
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    monkeypatch.setattr(runner, "kick_tick", lambda root: None)
    monkeypatch.setattr(runner, "_execute", lambda n, ctx, worktree: (_ for _ in ()).throw(OwnershipLost("#1 is Bob's now")))

    assert runner.run_issue(1, case.ctx) == "refused"

    assert capsys.readouterr().err.splitlines() == ["#1 is Bob's now"]
