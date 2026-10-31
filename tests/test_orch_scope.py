"""The shared person boundary for this factory's effects (#1004)."""
from types import SimpleNamespace

from foragent import orch_scope
from foragent.config import Config
from foragent.ownership import OWNED_MARKER
from foragent.state import StateStore
from foragent.tracker import FakeTracker, Issue, RecordingTracker, TrackerError


def issue(*labels, number=7, body=""):
    return Issue(number, "Work", body, frozenset(labels), "open")


class CountingTracker(FakeTracker):
    def __init__(self, login="alice"):
        super().__init__(login=login)
        self.identity_reads = 0

    def authenticated_login(self, *, refresh=False):
        self.identity_reads += 1
        return super().authenticated_login(refresh=refresh)


def test_singleton_unlabelled_work_needs_no_identity_read():
    tracker = CountingTracker()
    assert orch_scope.foreign_reason(issue(), Config(), tracker) is None
    assert tracker.identity_reads == 0


def test_own_work_is_allowed_and_foreign_work_names_both_people():
    tracker = CountingTracker("alice")
    assert orch_scope.foreign_reason(issue("by:Alice"), Config(), tracker) is None
    refusal = orch_scope.foreign_reason(issue("by:bob"), Config(), tracker)
    assert refusal == "#7 is worked by bob; this factory is alice."


def test_legacy_owner_decides_unlabelled_work():
    config = Config(legacy_owner="alice")
    assert orch_scope.foreign_reason(issue(), config, CountingTracker("alice")) is None
    assert "alice" in orch_scope.foreign_reason(issue(), config, CountingTracker("bob"))


def test_doubt_refuses_ambiguous_unconfirmed_and_unknown_identity():
    tracker = CountingTracker("alice")
    assert orch_scope.foreign_reason(issue("by:alice", "by:bob"), Config(), tracker) is not None
    assert orch_scope.foreign_reason(issue(body=OWNED_MARKER), Config(), tracker) is not None
    nobody = FakeTracker(login="")
    assert orch_scope.foreign_reason(issue("by:alice"), Config(), nobody) is not None
    assert orch_scope.factory_login(nobody) is None


def test_scope_reads_identity_once_per_tick():
    tracker = CountingTracker("alice")
    scope = orch_scope.Scope(SimpleNamespace(tracker=tracker, config=Config()))
    assert [scope.owns(issue("by:alice", number=n)) for n in (1, 2)] == [True, True]
    assert not scope.owns(issue("by:bob"))
    assert tracker.identity_reads == 1


def test_merge_resolves_the_pr_to_its_issue_before_comparing():
    tracker = FakeTracker(login="alice")
    tracker.add_issue(3, "Bob's", "", labels=["by:bob"])
    pr = tracker.create_pr(head="foragent/issue-3", base="main", title="t", body="b")
    ctx = SimpleNamespace(tracker=tracker, config=Config(legacy_owner="alice"),
                          store=SimpleNamespace(local_issues=lambda: []))
    assert pr != 3
    assert orch_scope.refuse_verb(ctx, "merge", pr) == "#3 is worked by bob; this factory is alice."
    assert orch_scope.refuse_verb(ctx, "list", None) is None


def test_a_failed_owner_read_refuses_the_verb_instead_of_allowing_it():
    class Failing(FakeTracker):
        def get_issue(self, n):
            raise TrackerError("HTTP 502")

    ctx = SimpleNamespace(tracker=Failing(login="alice"), config=Config(legacy_owner="alice"),
                          store=SimpleNamespace(local_issues=lambda: []))
    refusal = orch_scope.refuse_verb(ctx, "retry", 1)
    assert refusal is not None and "#1" in refusal and "502" in refusal
    assert orch_scope.may_write(ctx, 1) is False


def flush_case(tmp_path, *issues, legacy=None):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 31, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: now)
    tracker = FakeTracker(login="alice")
    for number, labels in issues:
        tracker.add_issue(number, f"#{number}", "", labels=labels)
    context = SimpleNamespace(store=store, now=lambda: now, config=Config(legacy_owner=legacy))
    recorder = RecordingTracker(tracker, context, queue_writes=True)
    context.tracker = recorder
    store.refresh_local_issues([tracker.get_issue(n) for n, _ in issues], [], {}, now)  # The tick's listing.
    return store, tracker, recorder, now


def test_refused_replay_moves_aside_with_its_reason_and_frees_other_writes(tmp_path):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, ["by:bob"]), (2, ["by:alice"]))
    store.queue_github_write(1, "comment", body="Foreign")
    store.queue_github_write(2, "comment", body="Mine")
    recorder.flush_outbox()
    recorder.flush_outbox()
    assert [c.body for c in tracker.list_comments(2)] == ["Mine"] and tracker.list_comments(1) == []
    assert store.github_outbox() == [], "A refusal must not stay a global barrier for starts and writes"
    [kept] = store.refused_github_writes()
    assert kept["issue"] == 1 and kept["body"] == "Foreign" and "bob" in kept["refused"]
    events = (store.root / "orch-events.jsonl").read_text().splitlines()
    assert sum('"outbox-refused"' in line for line in events) == 1
    recorder.comment(2, "Later")  # The next owned write goes straight out.
    assert [c.body for c in tracker.list_comments(2)] == ["Mine", "Later"]


def test_an_owner_label_edit_during_the_queue_keeps_the_write_refused_not_dropped(tmp_path):
    store, tracker, recorder, now = flush_case(tmp_path, (1, []))
    store.queue_github_write(1, "comment", body="Queued before the edit")
    tracker.add_labels(1, ["by:bob"])
    recorder.refresh_issues(observed_at=now)
    recorder.flush_outbox()
    assert tracker.list_comments(1) == []
    [kept] = store.refused_github_writes()
    assert kept["body"] == "Queued before the edit" and "by:bob" in kept["refused"]


def test_an_ordinary_label_edit_still_drops_the_queued_write(tmp_path):
    store, tracker, recorder, now = flush_case(tmp_path, (1, []))
    store.queue_github_write(1, "add_labels", labels=["ready"])
    tracker.add_labels(1, ["blocked"])
    recorder.refresh_issues(observed_at=now)
    assert store.github_outbox() == [] and store.refused_github_writes() == []


def test_a_queued_pr_comment_follows_the_issue_the_pr_works(tmp_path):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, ["by:bob"]), legacy="alice")
    pr = tracker.create_pr(head="foragent/issue-1", base="main", title="t", body="b")
    store.queue_github_write(pr, "comment", body="On Bob's PR")
    recorder.flush_outbox()
    assert tracker.list_comments(pr) == []
    assert [entry["issue"] for entry in store.refused_github_writes()] == [pr]


def test_question_addresses_the_effective_orderer_only_outside_the_singleton_path():
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Legacy", "", labels=[])
    tracker.add_issue(2, "Bob's", "", labels=["by:bob"])
    ctx = SimpleNamespace(tracker=tracker, config=Config())
    assert orch_scope.ask_orderer(ctx, 1, "Decide.") == "Decide."
    assert orch_scope.ask_orderer(ctx, 2, "Decide.") == "@bob Decide."
    legacy = SimpleNamespace(tracker=tracker, config=Config(legacy_owner="carol"))
    assert orch_scope.ask_orderer(legacy, 1, "Decide.") == "@carol Decide."


def test_singleton_replay_trusts_this_ticks_listing_without_another_read(tmp_path):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, []))
    reads = []
    original = tracker.get_issue
    tracker.get_issue = lambda n: reads.append(n) or original(n)
    store.queue_github_write(1, "comment", body="Queued")
    recorder.flush_outbox()
    assert [c.body for c in tracker.list_comments(1)] == ["Queued"] and reads == []


def test_question_still_posts_when_the_issue_cannot_be_read():
    class Failing(FakeTracker):
        def get_issue(self, n):
            raise TrackerError("down")

    ctx = SimpleNamespace(tracker=Failing(login="alice"), config=Config())
    assert orch_scope.ask_orderer(ctx, 1, "Decide.") == "Decide."


def test_parent_aggregation_rereads_the_owner_before_closing(monkeypatch):
    from datetime import UTC, datetime
    from foragent import board, board_parents

    closed = []
    # The stored rows still say Alice for #1, and #5 has no row's owner at all; GitHub now decides.
    rows = [{"number": 1, "state": "open", "labels": ["by:alice"], "children": [2]},
            {"number": 3, "state": "open", "labels": ["by:alice"], "children": [4]}]
    done = lambda n: Issue(n, "", "", frozenset(), "closed", state_reason="completed")
    tracker = FakeTracker(login="alice")
    tracker.add_issue(1, "Relabelled", "", labels=["by:bob"])
    tracker.add_issue(3, "Mine", "", labels=["by:alice"])
    monkeypatch.setattr(tracker, "sub_issues", lambda n: [done(n + 1)])
    monkeypatch.setattr(tracker, "close_issue", lambda n, comment: closed.append(n))
    moved = []
    monkeypatch.setattr(board, "sync_status", lambda ctx, n, state, **kw: moved.append((n, state)))
    store = SimpleNamespace(local_issues=lambda: rows, load_flag=lambda n: None)
    ctx = SimpleNamespace(store=store, tracker=tracker, config=Config(),
                          now=lambda: datetime(2026, 10, 31, tzinfo=UTC))
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert closed == [3], "A parent relabelled to Bob after the listing is his factory's to close"
    assert moved == [(3, "Done")], "A refused close also leaves Bob's card where it is"


def test_without_a_team_setting_known_facts_decide_and_nothing_is_read_again():
    class Counting(FakeTracker):
        reads = 0

        def get_issue(self, n):
            Counting.reads += 1
            return super().get_issue(n)

    tracker = Counting(login="alice")
    tracker.add_issue(1, "Legacy", "", labels=[])
    tracker.add_issue(2, "Bob's", "", labels=["by:bob"])
    rows = [{"number": 2, "labels": ["by:bob"]}]
    ctx = SimpleNamespace(tracker=tracker, config=Config(), store=SimpleNamespace(local_issues=lambda: rows))
    assert orch_scope.may_write(ctx, 1) and Counting.reads == 0
    assert not orch_scope.may_write(ctx, 2), "A stored foreign owner still refuses"


def test_replay_rechecks_the_owner_before_every_queued_write(tmp_path, monkeypatch):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, ["by:alice"]), legacy="alice")
    store.queue_github_write(1, "comment", body="First")
    store.queue_github_write(1, "close_issue", body="Done")
    comment = tracker.comment

    def comment_then_handover(n, body):
        result = comment(n, body)
        tracker.remove_labels(1, ["by:alice"])
        tracker.add_labels(1, ["by:bob"])
        return result

    monkeypatch.setattr(tracker, "comment", comment_then_handover)
    recorder.flush_outbox()
    assert [c.body for c in tracker.list_comments(1)] == ["First"]
    assert tracker.get_issue(1).state == "open", "The close queued after the handover is Bob's to decide"
    assert [entry["action"] for entry in store.refused_github_writes()] == ["close_issue"]


def test_a_refresh_keeps_queued_pr_writes_for_replay(tmp_path):
    store, tracker, recorder, now = flush_case(tmp_path, (1, ["by:alice"]), legacy="alice")
    pr = tracker.create_pr(head="foragent/issue-1", base="main", title="t", body="b")
    store.queue_github_write(pr, "comment", body="On my PR")
    recorder.refresh_issues(observed_at=now)
    assert [entry["issue"] for entry in store.github_outbox()] == [pr], "A PR is not a missing issue"
    recorder.flush_outbox()
    assert [c.body for c in tracker.list_comments(pr)] == ["On my PR"]


def test_an_unresolvable_pr_write_is_refused_not_delivered(tmp_path, monkeypatch):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, ["by:bob"]), legacy="alice")
    pr = tracker.create_pr(head="foragent/issue-1", base="main", title="t", body="b")
    monkeypatch.setattr(tracker, "list_prs", lambda state="open", head_prefix="": [])  # Closed, never merged.
    store.queue_github_write(pr, "comment", body="On a closed PR")
    recorder.flush_outbox()
    assert tracker.list_comments(pr) == []
    [kept] = store.refused_github_writes()
    assert kept["issue"] == pr and f"#{pr}" in kept["refused"]


def test_one_unreadable_target_is_refused_and_other_writes_still_go_out(tmp_path, monkeypatch):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, ["by:alice"]), (2, ["by:alice"]), legacy="alice")
    get_issue = tracker.get_issue
    monkeypatch.setattr(tracker, "get_issue", lambda n: (_ for _ in ()).throw(TrackerError("gh: HTTP 403"))
                        if n == 1 else get_issue(n))
    store.queue_github_write(1, "comment", body="Unreadable target")
    store.queue_github_write(2, "comment", body="Mine")
    recorder.flush_outbox()
    assert [c.body for c in tracker.list_comments(2)] == ["Mine"]
    [kept] = store.refused_github_writes()
    assert kept["issue"] == 1 and "403" in kept["refused"]


def test_a_malformed_owner_answer_is_no_permission_and_never_raises():
    class Malformed(FakeTracker):
        def get_issue(self, n):
            raise ValueError("Expecting property name enclosed in double quotes")

    ctx = SimpleNamespace(tracker=Malformed(login="alice"), config=Config(legacy_owner="alice"),
                          store=SimpleNamespace(local_issues=lambda: []))
    assert orch_scope.may_write(ctx, 1) is False
    assert "#1" in orch_scope.current_refusal(ctx, 1)


def test_the_write_boundary_rereads_a_stale_cached_owner_before_any_label_or_comment(tmp_path):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, ["by:alice", "blocked"]), legacy="alice")
    tracker.remove_labels(1, ["by:alice"])  # Relabelled to Bob after the last listing.
    tracker.add_labels(1, ["by:bob"])
    recorder.add_labels(1, ["needs-human"])
    recorder.comment(1, "<!-- foragent:needs-human reason=orchestrator-timeout -->")
    recorder.close_issue(1, "closing")
    issue = tracker.get_issue(1)
    assert "needs-human" not in issue.labels and issue.state == "open" and tracker.list_comments(1) == []
    row = next(row for row in store.local_issues() if row["number"] == 1)
    assert "needs-human" not in row["labels"] and row["state"] == "open" and row["comments"] == []
    events = (store.root / "orch-events.jsonl").read_text()
    assert events.count('"write-refused"') == 3


def test_the_write_boundary_lets_own_writes_through(tmp_path):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, ["by:alice"]), legacy="alice")
    recorder.add_labels(1, ["needs-human"])
    recorder.comment(1, "Mine")
    assert "needs-human" in tracker.get_issue(1).labels and [c.body for c in tracker.list_comments(1)] == ["Mine"]


def test_a_queued_pr_comment_is_judged_by_its_linked_issue_without_a_team_setting(tmp_path):
    store, tracker, recorder, now = flush_case(tmp_path, (1, ["by:alice"]))
    pr = tracker.create_pr(head="foragent/issue-1", base="main", title="t", body="b")
    store.update_local_issue(1, pr_number=pr)
    tracker.login = ""  # The signed-in account was lost: this factory cannot prove it is Alice's.
    store.queue_github_write(pr, "comment", body="On the PR")
    recorder.flush_outbox()
    assert tracker.list_comments(pr) == []
    assert [entry["issue"] for entry in store.refused_github_writes()] == [pr]


def test_a_refused_queued_close_leaves_the_local_issue_open(tmp_path):
    store, tracker, recorder, _ = flush_case(tmp_path, (1, ["by:alice"]), legacy="alice")
    store.queue_github_write(1, "close_issue", body="Done")
    assert next(row for row in store.local_issues() if row["number"] == 1)["state"] == "closed"
    tracker.remove_labels(1, ["by:alice"])
    tracker.add_labels(1, ["by:bob"])
    recorder.flush_outbox()
    assert tracker.get_issue(1).state == "open"
    row = next(row for row in store.local_issues() if row["number"] == 1)
    assert row["state"] == "open", "Nothing was sent, so the local row returns to GitHub's facts"
