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
def test_a_failed_entry_read_on_an_issue_cached_unlabelled_does_nothing(git_history, tmp_path, monkeypatch, effect):
    from foragent import orch_scope, runner
    from foragent.tracker import TrackerError

    case = team_run_case(git_history, tmp_path, monkeypatch, [], ["by:bob"])  # Cached unlabelled; Bob's now.
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


@pytest.mark.parametrize("actual, flagged", [(["by:bob"], False), (["by:alice"], True), ([], True)])
@pytest.mark.parametrize("failure", ["HTTP 502", "HTTP 422 Validation Failed", "HTTP 404 Not Found"])
def test_after_one_failed_entry_read_only_a_new_read_decides(git_history, tmp_path, monkeypatch, failure, actual, flagged):
    """Nothing cached counts; a later read that answers decides, for a locked 422 too."""
    from foragent import orch_scope, runner
    from foragent.tracker import TrackerError

    case = team_run_case(git_history, tmp_path, monkeypatch, [], actual)
    real = orch_scope.observe
    answers = iter([SimpleNamespace(get_issue=lambda n: (_ for _ in ()).throw(TrackerError(failure)))])
    monkeypatch.setattr(orch_scope, "observe", lambda tracker: next(answers, None) or real(tracker))

    assert runner.run_issue(1, case.ctx) == "error"
    labels = case.tracker.get_issue(1).labels
    assert ("blocked" in labels) is flagged
    assert bool(case.tracker.list_comments(1)) is flagged


def test_a_successful_entry_read_on_a_singleton_issue_still_reports_a_later_failure(
    git_history, tmp_path, monkeypatch,
):
    from foragent import runner

    case = team_run_case(git_history, tmp_path, monkeypatch, [], [])
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    monkeypatch.setattr(runner, "kick_tick", lambda root: None)
    monkeypatch.setattr(runner, "_execute", lambda n, ctx, worktree: (_ for _ in ()).throw(RuntimeError("boom")))

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


def pr_chain(tracker, owner_labels, links=2):
    """Issue #1, then a chain of PRs where each one's branch names the previous number."""
    tracker.add_issue(1, "Work", "", labels=owner_labels)
    number = 1
    for _ in range(links):
        number = tracker.create_pr(head=f"foragent/issue-{number}", base="main", title="Link", body="")
    return number


@pytest.mark.parametrize("links", [1, 2, 3])
@pytest.mark.parametrize("owner, allowed", [("bob", False), ("alice", True)])
def test_an_uncached_pr_chain_is_judged_by_the_issue_at_its_end(tmp_path, links, owner, allowed):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    head = pr_chain(tracker, [f"by:{owner}"], links)
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config())
    recorder = RecordingTracker(tracker, context, queue_writes=True)
    context.tracker = recorder
    store.queue_github_write(head, "comment", body="Queued")

    recorder.flush_outbox()

    assert ([c.body for c in tracker.list_comments(head)] == ["Queued"]) is allowed
    assert bool(store.refused_github_writes()) is not allowed

    from foragent.tracker import WriteRefused

    for operation in (lambda: recorder.update_pr(head, base="main", title="T", body="B"),
                      lambda: recorder.close_pr(head),
                      lambda: recorder.merge_pr(head, expected_head_sha="x")):
        if allowed:
            continue
        with pytest.raises(WriteRefused):
            operation()
    if not allowed:
        assert tracker.get_issue(head).state == "open" and tracker.merged_prs == []


def test_a_pr_chain_with_a_missing_end_or_a_cycle_refuses(tmp_path):
    from foragent import orch_scope
    from foragent.tracker import TrackerError

    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Work", "")
    first = tracker.create_pr(head="foragent/issue-1", base="main", title="A", body="")
    read = tracker.get_issue

    def without_issue(n):
        if n == 1:
            raise TrackerError("HTTP 404 Not Found")
        return read(n)

    tracker.get_issue = without_issue
    with pytest.raises(TrackerError, match="404"):
        orch_scope._resolve_issue(None, tracker, first)

    cycle = FakeTracker(login="alice")
    a = cycle.create_pr(head="foragent/issue-2", base="main", title="A", body="")
    b = cycle.create_pr(head=f"foragent/issue-{a}", base="main", title="B", body="")
    assert b == 2
    with pytest.raises(orch_scope.Unresolved):
        orch_scope._resolve_issue(None, cycle, a)


def test_entry_and_write_lookups_share_one_chain_resolver(tmp_path):
    from foragent import orch_scope

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    head = pr_chain(tracker, ["by:bob"], 2)
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config(), tracker=tracker)

    assert orch_scope.current_issue(context, head).number == 1
    assert "bob" in orch_scope.check(context, head, tracker.get_issue(head))  # The merge preflight's read of a PR.
    context._scope = None
    assert "bob" in orch_scope.merge_check(context, head, tracker.get_issue(head))
    context._scope = None
    assert "bob" in orch_scope.scope(context).refusal(tracker.get_issue(head))


@pytest.mark.parametrize("labels, cached, mention", [
    ([], True, "@alice"), ([], False, "@alice"), (["by:bob"], True, "@bob"), (["by:alice"], False, "@alice"),
])
def test_every_issue_question_mentions_its_effective_orderer(tmp_path, labels, cached, mention):
    from foragent import orch_scope

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Work", "", labels=labels)
    if cached:
        store.record_issue(tracker.get_issue(1), {})
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config(), tracker=tracker)

    assert orch_scope.ask_orderer(context, 1, "Which one?") == f"{mention} Which one?"


def signed_out(tracker):
    tracker.authenticated_login = lambda: (_ for _ in ()).throw(RuntimeError("signed out"))


def test_no_question_text_exists_without_an_orderer_and_its_details_stay_in_the_log(tmp_path):
    from foragent import orch_scope

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Work", "")
    store.record_issue(tracker.get_issue(1), {})
    signed_out(tracker)
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config(), tracker=tracker)

    with pytest.raises(orch_scope.OrdererUnknown, match="#1"):
        orch_scope.ask_orderer(context, 1, "Which one?", "The long question")
    assert "The long question" in store.log_path(1).read_text()


def test_a_tick_posts_no_question_until_it_can_name_the_orderer(git_history, tmp_path, monkeypatch):
    from foragent import scheduler
    from foragent.tracker import Issue
    from test_orch_review_handoff import make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch)
    case.store.finish_run(1, "blocked")
    case.tracker.remove_labels(1, ["running"])
    blocker = Issue(7, "Gone", "", frozenset(), "closed", state_reason="not_planned")
    monkeypatch.setattr(scheduler, "_blockers", lambda ctx, n, confirmed=False: [blocker])
    login = case.tracker.authenticated_login
    signed_out(case.tracker)
    ctx = tick_context(case)
    issue, approval = case.tracker.get_issue(1), case.store.load_approval(1)
    before = shared_effects(case)
    report = scheduler.TickReport()

    assert scheduler._check_prerequisites(ctx, issue, approval, report) is False
    assert shared_effects(case) == before
    assert any("#1" in action for action in report.actions)

    case.tracker.authenticated_login = login
    ctx._scope = None  # The next tick.
    assert scheduler._check_prerequisites(ctx, issue, approval, scheduler.TickReport()) is False
    assert any("#7" in c.body and c.body.count("@") for c in case.tracker.list_comments(1))


@pytest.mark.parametrize("producer", ["executor", "reviewer"])
def test_a_run_question_is_not_posted_without_an_orderer(git_history, tmp_path, monkeypatch, producer):
    from foragent import run_review
    from foragent.orch_scope import OrdererUnknown
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    signed_out(case.tracker)
    before = shared_effects(case)
    if producer == "reviewer":
        review = SimpleNamespace(conflicts=("SPEC-CONFLICT: a locked test",))
        with pytest.raises(OrdererUnknown):
            run_review._stop_on_spec_conflict(1, case.ctx, review)
        assert "SPEC-CONFLICT: a locked test" in case.store.log_path(1).read_text()
    else:
        from foragent.orch_scope import ask_orderer

        with pytest.raises(OrdererUnknown):
            ask_orderer(case.ctx, 1, "The executor needs a product decision.", "details")
    assert shared_effects(case) == before


def test_an_unread_number_during_a_github_wait_gets_no_unaddressed_question(tmp_path):
    from datetime import timedelta
    from foragent import orch_scope

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Work", "")
    store.pause_github(NOW + timedelta(minutes=1))
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config(), tracker=tracker)

    with pytest.raises(orch_scope.OrdererUnknown):
        orch_scope.ask_orderer(context, 1, "Which one?")


def absent_read(tracker):
    from foragent.tracker import TrackerError

    read = tracker.get_issue

    def missing(n):
        if n == 1:
            raise TrackerError("HTTP 404 Not Found")
        return read(n)

    tracker.get_issue = missing
    return read


def test_a_read_that_finds_nothing_decides_nothing(tmp_path):
    from foragent import orch_scope

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Bob's", "", labels=["by:bob"])
    read = absent_read(tracker)
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config())
    context.tracker = RecordingTracker(tracker, context)

    assert orch_scope.may_write(context, 1) is False
    tracker.get_issue = read
    assert orch_scope.may_write(context, 1) is False  # The earlier 404 left no permission behind.
    assert tracker.list_comments(1) == []


def waiting_case(tmp_path, *, queue_writes):
    from datetime import timedelta

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Bob's parent", "", labels=["by:bob"])
    store.pause_github(NOW + timedelta(minutes=1))
    context = SimpleNamespace(store=store, now=lambda: NOW, config=Config(), repo="owner/repo")
    recorder = RecordingTracker(tracker, context, queue_writes=queue_writes)
    context.tracker = recorder
    return store, tracker, recorder, context


def test_a_github_wait_lets_no_direct_write_reach_an_unread_target(tmp_path):
    from foragent.tracker import WriteRefused

    store, tracker, recorder, _ = waiting_case(tmp_path, queue_writes=False)
    pr = tracker.create_pr(head="foragent/issue-1", base="main", title="T", body="B")

    recorder.comment(1, "Direct")
    recorder.add_labels(1, ["blocked"])
    for operation in (lambda: recorder.create_pr(head="foragent/batch-1", base="main", title="T", body="B"),
                      lambda: recorder.update_pr(pr, base="main", title="X", body="Y"),
                      lambda: recorder.close_pr(pr),
                      lambda: recorder.merge_pr(pr, expected_head_sha="x"),
                      lambda: recorder.add_sub_issue(1, 2)):
        with pytest.raises(WriteRefused):
            operation()

    assert tracker.list_comments(1) == [] and "blocked" not in tracker.get_issue(1).labels
    assert [p.number for p in tracker.created_prs] == [pr] and tracker.merged_prs == []
    assert recorder.board_write_refused(1) is True
    assert store.github_outbox() == []


def test_a_wait_refusal_is_not_a_decision_and_a_queue_entry_waits_for_replay(tmp_path):
    from datetime import timedelta
    from foragent import orch_scope

    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Mine", "", labels=["by:alice"])
    context = SimpleNamespace(store=store, now=lambda: clock[0], config=Config())
    recorder = RecordingTracker(tracker, context, queue_writes=True)
    context.tracker = recorder
    store.pause_github(NOW + timedelta(minutes=1))

    assert orch_scope.may_write(context, 1) is False
    recorder.comment(1, "Queued")
    assert [entry["body"] for entry in store.github_outbox()] == ["Queued"]
    assert tracker.list_comments(1) == [] and store.refused_github_writes() == []

    clock[0] = NOW + timedelta(minutes=2)
    assert orch_scope.may_write(context, 1) is True
    recorder.flush_outbox()
    assert [c.body for c in tracker.list_comments(1)] == ["Queued"]


def test_a_replay_during_a_wait_reads_before_it_sends(tmp_path):
    store, tracker, recorder, _ = waiting_case(tmp_path, queue_writes=True)
    recorder.comment(1, "Queued on an unread target")

    recorder.flush_outbox()

    assert tracker.list_comments(1) == []
    assert [entry["body"] for entry in store.refused_github_writes()] == ["Queued on an unread target"]


def test_a_replay_during_a_wait_does_not_discard_an_owned_write_it_can_read(tmp_path):
    store, tracker, recorder, _ = waiting_case(tmp_path, queue_writes=True)
    tracker.remove_labels(1, ["by:bob"])
    tracker.add_labels(1, ["by:alice"])
    recorder.comment(1, "Mine")

    recorder.flush_outbox()

    assert [c.body for c in tracker.list_comments(1)] == ["Mine"]
    assert store.refused_github_writes() == []
