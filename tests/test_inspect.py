from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
import json
import multiprocessing
import subprocess

import pytest

from foragent.notify import macos_notifier
from foragent.config import Config
from foragent.inspect_actions import InspectError, answer, feedback, mark_seen, notify, retry
from foragent.inspect_data import GROUPS, Inbox, InspectContext, collect, pending_signature
from foragent.reasons import GROUP, Reason, format_comment
from foragent.state import StateStore
from foragent.tracker import Comment, FakeTracker, TrackerError


NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)


@pytest.fixture
def ctx(tmp_path):
    return InspectContext(
        FakeTracker(strict=True, now=lambda: NOW), StateStore(tmp_path / "state"),
        "owner/repo", Config(), lambda: NOW,
    )


def flag(ctx, number=1, reason=Reason.SPEC_QUESTION, details="Question?\n1. A (recommended)\n2. B"):
    ctx.tracker.add_issue(number, f"Issue {number}", "Original body", ["needs-human"])
    ctx.tracker.comment(number, format_comment(reason, f"Summary {number}", details))


def _notify_in_process(root, connection, pause_delivery, fail_delivery):
    ctx = InspectContext(FakeTracker(now=lambda: NOW), StateStore(root), "owner/repo", Config(), lambda: NOW)
    flag(ctx)

    def notifier(line, title):
        connection.send(("delivering", line))
        if pause_delivery:
            if not connection.poll(10):
                raise TimeoutError("notification was not resumed")
            assert connection.recv() == "continue"
        if fail_delivery:
            raise TrackerError("delivery failed")

    try:
        connection.send(("started", None))
        try:
            result = notify(ctx, notifier)
        except TrackerError as exc:
            connection.send(("error", str(exc)))
        else:
            connection.send(("result", result))
    finally:
        connection.close()


@pytest.fixture
def notification_process():
    context = multiprocessing.get_context("spawn")
    children = []

    def start(root, *, pause_delivery=False, fail_delivery=False):
        parent, child = context.Pipe()
        process = context.Process(target=_notify_in_process, args=(root, child, pause_delivery, fail_delivery))
        process.start()
        child.close()
        children.append((process, parent))
        assert parent.poll(10)
        assert parent.recv() == ("started", None)
        return parent

    yield start
    for process, connection in children:
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        connection.close()
        assert process.exitcode == 0


def pr(ctx, number=1, *, body="Closes #1", merged=False, head=None, at=NOW):
    result = ctx.tracker.create_pr(
        head=head or f"foragent/issue-{number}", base="main", title=f"PR for {number}", body=body,
    )
    if merged:
        ctx.tracker._now = lambda: at
        ctx.tracker.merge_pr(result, expected_head_sha=ctx.tracker.pr_head_sha(result))
        ctx.tracker._now = lambda: NOW
    return result


def test_collect_groups_every_reason_orders_and_only_reads(ctx):
    for number, reason in reversed(list(enumerate(Reason, 1))):
        flag(ctx, number, reason)
    flag(ctx, 20)
    flag(ctx, 21, Reason.RETRIES_EXCEEDED)
    ctx.tracker.add_issue(99, labels=["needs-human"], state="closed")
    ctx.tracker.add_issue(100, labels=["needs-human"], is_pull_request=True)
    ctx.tracker.add_issue(101)
    original_issues = dict(ctx.tracker.issues)
    original_comments = {n: list(comments) for n, comments in ctx.tracker.comments.items()}
    inbox = collect(ctx)
    expected = [(GROUP[reason], n) for n, reason in enumerate(Reason, 1)]
    expected += [("decision", 20), ("tech-failure", 21)]
    assert [(item.group, item.number) for item in inbox.items] == sorted(
        expected, key=lambda item: (GROUPS.index(item[0]), item[1]),
    )
    assert list(inbox.counts) == list(GROUPS)
    assert sum(inbox.counts.values()) == len(Reason) + 2
    assert inbox.since == (NOW - timedelta(days=7)).isoformat()
    for item in inbox.items:
        assert item.summary == f"Summary {item.number}"
        assert item.details.startswith("Question?") and not item.answered
        assert item.url == f"https://github.com/owner/repo/issues/{item.number}"
    assert ctx.tracker.issues == original_issues and ctx.tracker.comments == original_comments
    assert not ctx.store.root.exists()


def test_latest_first_line_marker_summary_details_limit_and_fallback(ctx):
    flag(ctx, 1, details="x" * 5000)
    ctx.tracker.comment(1, "text\n" + format_comment(Reason.HASH_MISMATCH, "Not a first-line marker"))
    ctx.tracker.comment(1, "")
    ctx.tracker.add_issue(2, title="Unmarked", labels=["needs-human"])
    ctx.tracker.comment(2, "Unrelated comment")
    inbox = collect(ctx)
    assert inbox.items[0].summary == "Summary 1"
    assert len(inbox.items[0].details) == 4000
    assert inbox.items[1].summary == "Needs a human (no Foragent comment found)"
    assert inbox.items[1].reason is None and inbox.items[1].group == "decision"
    ctx.tracker.comment(1, format_comment(Reason.HASH_MISMATCH, "Changed", "A diff"))
    # GitHub comments can have equal timestamps; IDs break ties.
    ctx.tracker.comments[1].reverse()
    changed = next(item for item in collect(ctx).items if item.number == 1)
    assert (changed.group, changed.summary, changed.details) == ("reapprove", "Changed", "A diff")


def test_unknown_latest_reason_is_preserved_as_a_decision(ctx):
    flag(ctx)
    ctx.tracker.comment(1, "<!-- foragent:needs-human reason=future-reason -->\n**New reason**\n\nDetails")
    item, = collect(ctx).items
    assert (item.group, item.reason, item.summary) == ("decision", "future-reason", "New reason")


def test_answered_requires_a_first_line_decision_newer_than_latest_flag(ctx):
    ctx.tracker.comment(1, "<!-- foragent:decision -->\nOld answer")
    flag(ctx)
    ctx.tracker.comment(1, "Other text\n<!-- foragent:decision -->\nNot an answer")
    assert not collect(ctx).items[0].answered
    # Chronology must use dates, rather than lexical ordering of offsets or list order.
    ctx.tracker.comments[1].append(Comment(
        50, "human", "<!-- foragent:decision -->\nNew answer", "2026-10-20T19:00:00+09:00",
    ))
    assert collect(ctx).items[0].answered
    ctx.tracker._now = lambda: NOW + timedelta(hours=2)
    ctx.tracker.comment(1, format_comment(Reason.SPEC_QUESTION, "A new question"))
    assert not collect(ctx).items[0].answered


@pytest.mark.parametrize("mode", ["fast", "batch", "careful"])
def test_open_pr_review_modes_exclude_flagged_issues_and_unrelated_heads(ctx, mode):
    ctx.config = replace(ctx.config, review=mode)
    flag(ctx, 1, Reason.ALWAYS_HUMAN_PATH)
    flagged_pr = pr(ctx, 1)
    unflagged_pr = pr(ctx, 3)
    pr(ctx, head="someone/feature")
    pr(ctx, head="foragent/issue-4-extra")
    closed_pr = pr(ctx, 5)
    ctx.tracker.close_issue(closed_pr)
    inbox = collect(ctx)
    reviews = [item for item in inbox.items if item.group == "review"]
    assert len(reviews) == 2
    assert reviews[0].pr == flagged_pr
    assert reviews[0].url == f"https://github.com/owner/repo/pull/{flagged_pr}"
    assert (reviews[1].number, reviews[1].pr, reviews[1].reason) == (3, unflagged_pr, None)


def test_parent_batch_pr_appears_once_under_review(ctx):
    ctx.tracker.add_issue(99, "Epic")
    number = ctx.tracker.create_pr(
        head="foragent/batch-99", base="main", title="Batch: Epic", body="Closes #99\n\nChildren",
    )
    item, = collect(ctx).items
    assert item.group == "review" and item.number == 99 and item.pr == number
    assert item.url == f"https://github.com/owner/repo/pull/{number}"


def test_fast_mode_path_review_is_collected_and_notified(ctx):
    ctx.config = replace(ctx.config, review="fast", review_paths={"src/**": "careful"})
    ctx.tracker.add_issue(1, "Path review")
    number = pr(ctx)
    ctx.tracker.set_pr_files(number, ["src/app.py"])
    item, = collect(ctx).items
    assert (item.group, item.number, item.pr) == ("review", 1, number)
    calls = []
    assert notify(ctx, lambda *args: calls.append(args)) == "Review 1 · #1 PR for 1"
    assert calls == [("Review 1 · #1 PR for 1", "Foragent owner/repo")]


def test_merged_pr_since_checklists_and_one_try_per_issue(ctx):
    body = """- [ ] Outside the section
## Needs a human check
- [ ] Check keyboard input
- [x] Already checked
### More checks
* [ ] Check mobile layout
```md
- [ ] Example only
```
## Other section
- [ ] Unrelated
"""
    old_pr = pr(ctx, 1, merged=True, at=NOW - timedelta(days=8))
    boundary_pr = pr(ctx, 1, merged=True, at=NOW - timedelta(days=7))
    first = pr(ctx, 7, body=body, merged=True)
    second = pr(ctx, 7, merged=True)
    pr(ctx, head="other/issue-8", merged=True)
    pr(ctx, head="foragent/issue-8-extra", merged=True)
    inbox = collect(ctx)
    assert [(item.group, item.number, item.pr) for item in inbox.items] == [
        ("human-check", 7, first), ("try", 7, second),
    ]
    assert inbox.items[0].details == "- [ ] Check keyboard input\n* [ ] Check mobile layout"
    assert all(item.url == f"https://github.com/owner/repo/pull/{item.pr}" for item in inbox.items)
    ctx.store.update_inspect(since=(NOW - timedelta(days=9)).isoformat())
    assert {item.pr for item in collect(ctx).items if item.group == "try"} == {boundary_pr, second}
    assert old_pr not in {item.pr for item in collect(ctx).items}
    mark_seen(ctx)
    assert collect(ctx).items == []


@pytest.mark.parametrize("reverse", [False, True])
def test_try_uses_latest_merge_time_per_issue_and_has_no_body(ctx, reverse):
    latest = pr(ctx, 7, body="Latest PR body\n" * 100, merged=True)
    older = pr(ctx, 7, body="Older PR body", merged=True, at=NOW - timedelta(minutes=30))
    tied_first = pr(ctx, 9, merged=True)
    tied_last = pr(ctx, 9, merged=True)
    ctx.tracker.created_prs = [
        replace(item, title="Latest title", url="https://github.com/owner/repo/pull/latest")
        if item.number == latest else
        replace(item, merged_at="2026-10-20T17:30:00+09:00")
        if item.number == older else item
        for item in ctx.tracker.created_prs
    ]
    if reverse:
        ctx.tracker.created_prs.reverse()
    inbox = collect(ctx)
    assert inbox.counts["try"] == 2
    assert [(item.number, item.pr) for item in inbox.items] == [(7, latest), (9, tied_last)]
    assert inbox.items[0].title == "Latest title"
    assert inbox.items[0].url == "https://github.com/owner/repo/pull/latest"
    assert tied_first not in {item.pr for item in inbox.items}
    for item in inbox.items:
        assert item.summary == f"Merged in PR #{item.pr}; try it"
        assert item.details == ""


def test_answer_idempotent_within_question_keeps_labels_and_body(ctx):
    flag(ctx)
    original = ctx.tracker.get_issue(1)
    first = answer(ctx, 1, "A\nA note")
    assert answer(ctx, 1, " A\nA note ") == first
    assert ctx.tracker.list_comments(1)[-1].body == "<!-- foragent:decision -->\n**Decision:**\nA\nA note"
    assert collect(ctx).items[0].answered
    assert ctx.tracker.get_issue(1) == original
    ctx.tracker.comment(1, format_comment(Reason.SPEC_QUESTION, "New question"))
    assert answer(ctx, 1, "A\nA note") != first


@pytest.mark.parametrize("question", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_answer_only_reuses_the_latest_decision(ctx, question, reverse):
    if question:
        flag(ctx)
    else:
        ctx.tracker.add_issue(1)
    first = answer(ctx, 1, "A")
    second = answer(ctx, 1, "B")
    # List order and unrelated comments must not change the latest decision.
    ctx.tracker.comment(1, "Unrelated\n<!-- foragent:decision -->")
    if reverse:
        ctx.tracker.comments[1].reverse()
    third = answer(ctx, 1, "A")
    assert len({first, second, third}) == 3
    assert answer(ctx, 1, " A ") == third
    latest = max(ctx.tracker.list_comments(1), key=lambda comment: (comment.created_at, comment.id))
    assert latest.id == third and latest.body.endswith("\nA")


@pytest.mark.parametrize("reason", [Reason.RETRIES_EXCEEDED, Reason.TIMEOUT])
def test_retry_closes_existing_pr_deletes_branch_then_releases_hold(ctx, monkeypatch, reason):
    ctx.config = replace(ctx.config, review="fast")
    flag(ctx, reason=reason)
    number = pr(ctx)
    other = pr(ctx, head="foragent/issue-10")
    remove = ctx.tracker.remove_labels

    def remove_after_cleanup(issue, labels):
        assert ctx.tracker.get_issue(number).state == "closed"
        assert ctx.tracker.deleted_branches == ["foragent/issue-1"]
        assert ctx.tracker.list_comments(number)[-1].body == "Closed by Foragent retry"
        remove(issue, labels)

    monkeypatch.setattr(ctx.tracker, "remove_labels", remove_after_cleanup)
    retry(ctx, 1)
    retry(ctx, 1)
    assert ctx.tracker.find_pr("foragent/issue-1") is None
    assert ctx.tracker.get_issue(other).state == "open"
    assert "needs-human" not in ctx.tracker.get_issue(1).labels
    assert len(ctx.tracker.list_comments(1)) == 2
    assert len(ctx.tracker.list_comments(number)) == 1


def test_retry_pr_cleanup_failure_keeps_hold_and_can_retry(ctx, monkeypatch):
    flag(ctx, reason=Reason.TIMEOUT)
    number = pr(ctx)
    before = ctx.tracker.get_issue(1), ctx.tracker.list_comments(1)
    close = ctx.tracker.close_pr

    def fail(*args):
        raise TrackerError("PR cleanup failed")

    monkeypatch.setattr(ctx.tracker, "close_pr", fail)
    with pytest.raises(TrackerError, match="PR cleanup failed"):
        retry(ctx, 1)
    assert (ctx.tracker.get_issue(1), ctx.tracker.list_comments(1)) == before
    monkeypatch.setattr(ctx.tracker, "close_pr", close)
    retry(ctx, 1)
    assert ctx.tracker.get_issue(number).state == "closed"
    assert "needs-human" not in ctx.tracker.get_issue(1).labels


def test_repeated_retry_leaves_a_new_attempt_pr_open(ctx):
    flag(ctx, reason=Reason.TIMEOUT)
    closed = pr(ctx)
    retry(ctx, 1)
    new_attempt = pr(ctx)
    retry(ctx, 1)
    assert ctx.tracker.get_issue(closed).state == "closed"
    assert ctx.tracker.get_issue(new_attempt).state == "open"
    assert ctx.tracker.deleted_branches == ["foragent/issue-1"]
    assert len(ctx.tracker.list_comments(1)) == 2
    assert ctx.tracker.list_comments(new_attempt) == []


@pytest.mark.parametrize("reason", [Reason.RETRIES_EXCEEDED, Reason.STALLED, Reason.TIMEOUT])
def test_retry_tech_failure_idempotent_and_new_failure_can_be_retried(ctx, reason):
    flag(ctx, reason=reason)
    retry(ctx, 1)
    retry(ctx, 1)
    assert ctx.tracker.get_issue(1).labels == frozenset()
    assert ctx.tracker.get_issue(1).body == "Original body"
    assert [c.body for c in ctx.tracker.list_comments(1)][1:] == ["<!-- foragent:retry -->\nRetry requested."]
    ctx.tracker.add_labels(1, ["needs-human"])
    ctx.tracker.comment(1, format_comment(reason, "Failed again"))
    retry(ctx, 1)
    assert len(ctx.tracker.list_comments(1)) == 4


@pytest.mark.parametrize("reason", [r for r in Reason if GROUP[r] != "tech-failure"] + [None])
def test_retry_refuses_other_items_without_mutating(ctx, reason):
    flag(ctx, reason=reason or Reason.SPEC_QUESTION)
    if reason is None:
        ctx.tracker.comments[1] = []
    before = ctx.tracker.get_issue(1), ctx.tracker.list_comments(1)
    with pytest.raises(InspectError, match="not a tech-failure item"):
        retry(ctx, 1)
    assert (ctx.tracker.get_issue(1), ctx.tracker.list_comments(1)) == before


@pytest.mark.parametrize("state", ["closed", "unlabelled", "pr"])
def test_retry_refuses_inactive_items(ctx, state):
    flag(ctx, reason=Reason.TIMEOUT)
    if state == "closed":
        ctx.tracker.close_issue(1)
    elif state == "unlabelled":
        ctx.tracker.remove_labels(1, ["needs-human"])
    else:
        ctx.tracker.issues[1] = replace(ctx.tracker.issues[1], is_pull_request=True)
    with pytest.raises(InspectError, match="retry refused"):
        retry(ctx, 1)


def test_retry_can_resume_after_label_removal_failure(ctx, monkeypatch):
    flag(ctx, reason=Reason.STALLED)
    original = ctx.tracker.remove_labels
    monkeypatch.setattr(ctx.tracker, "remove_labels", lambda *a: (_ for _ in ()).throw(TrackerError("offline")))
    with pytest.raises(TrackerError):
        retry(ctx, 1)
    monkeypatch.setattr(ctx.tracker, "remove_labels", original)
    retry(ctx, 1)
    assert len(ctx.tracker.list_comments(1)) == 2
    assert "needs-human" not in ctx.tracker.get_issue(1).labels


def test_feedback_is_new_issue_with_context_and_open_duplicate_is_reused(ctx):
    flag(ctx)
    original = ctx.tracker.get_issue(1)
    number = feedback(ctx, "Layout", "Wrong spacing", 1)
    assert feedback(ctx, "Layout", "Wrong spacing", 1) == number
    issue = ctx.tracker.get_issue(number)
    assert (issue.title, issue.body, issue.labels) == ("Layout", "Wrong spacing\n\nAbout #1.", {"feedback"})
    assert ctx.tracker.get_issue(1) == original
    assert feedback(ctx, "Other", "No related issue") != number
    ctx.tracker.close_issue(number)
    assert feedback(ctx, "Layout", "Wrong spacing", 1) != number


@pytest.mark.parametrize("action,args", [(answer, (1, " \n")), (feedback, ("", "body")), (feedback, ("title", " "))])
def test_blank_input_refused_without_writes(ctx, action, args):
    with pytest.raises(InspectError, match="must not be empty"):
        action(ctx, *args)
    assert ctx.tracker.issues == {} and ctx.tracker.comments == {}


def test_mark_seen_and_signature_persist_independently(ctx):
    ctx.store.update_inspect(notified_signature="signature")
    assert mark_seen(ctx) == NOW.isoformat()
    reopened = StateStore(ctx.store.root)
    assert asdict(reopened.load_inspect()) == {"since": NOW.isoformat(), "notified_signature": "signature"}
    reopened.update_inspect(notified_signature="new")
    assert ctx.store.load_inspect().since == NOW.isoformat()
    assert json.loads((ctx.store.root / "inspect.json").read_text())["notified_signature"] == "new"


def test_signature_is_stable_and_only_uses_actionable_identity(ctx):
    flag(ctx)
    inbox = collect(ctx)
    signature = pending_signature(inbox)
    assert len(signature) == 64
    changed = replace(inbox.items[0], title="Other", details="Other", answered=True, summary="Other", url="Other")
    tried = replace(changed, group="try", number=7)
    assert pending_signature(Inbox([tried, changed], {}, None)) == signature
    for field in ({"number": 2}, {"reason": "no-machine-check"}, {"group": "review"}):
        assert pending_signature(Inbox([replace(changed, **field)], {}, None)) != signature
    assert pending_signature(Inbox([changed, replace(changed, number=2)], {}, None)) == pending_signature(
        Inbox([replace(changed, number=2), changed], {}, None),
    )


@pytest.mark.parametrize("channel", [None, "claude-push", "macos", "slack:https://example.test/hook"])
def test_notify_dedupes_signature_with_injected_delivery_and_counts(ctx, channel):
    ctx.config = replace(ctx.config, notify=channel)
    flag(ctx, 1)
    flag(ctx, 2)
    flag(ctx, 3, Reason.HASH_MISMATCH)
    flag(ctx, 4, Reason.RISK_HIGH)
    pr(ctx, 5, merged=True)
    calls = []
    notifier = lambda *args: calls.append(args)
    expected = ("Decision 2 · Re-approve 1 · Review 1 · Try 1 · "
                "#1 Issue 1 (spec-question: Summary 1) · #2 Issue 2 (spec-question: Summary 2) · "
                "#3 Issue 3 (hash-mismatch: Summary 3) · +1")
    assert notify(ctx, notifier) == expected
    assert calls == [
        (expected, "Foragent owner/repo"),
    ]
    assert notify(ctx, notifier) is None
    # Changes to try items or an answer do not change the pending identity.
    pr(ctx, 6, merged=True)
    answer(ctx, 1, "A")
    assert notify(ctx, notifier) is None
    flag(ctx, 9, Reason.STALLED)
    line = notify(ctx, notifier)
    assert line.startswith("Decision 2 · Re-approve 1 · Review 1 · Try 2 · Tech failure 1 · ")
    assert line.endswith("+2")


def test_notify_silent_for_empty_and_try_only_and_resets_resolved_signature(ctx):
    calls = []
    notifier = lambda *args: calls.append(args)
    assert notify(ctx, notifier) is None
    pr(ctx, 1, merged=True)
    assert notify(ctx, notifier) is None
    assert calls == []
    flag(ctx, 2)
    assert notify(ctx, notifier)
    ctx.tracker.remove_labels(2, ["needs-human"])
    assert notify(ctx, notifier) is None
    ctx.tracker.add_labels(2, ["needs-human"])
    assert notify(ctx, notifier)
    assert len(calls) == 2


def test_notification_flattens_titles_and_reasons_and_omits_try_items(ctx):
    ctx.tracker.add_issue(1, "Choose\nformat\tplease", labels=["needs-human"])
    ctx.tracker.comment(1, "<!-- foragent:needs-human reason=spec-question -->\n**Which\tformat?**")
    ctx.tracker.add_issue(2, "Unknown reason", labels=["needs-human"])
    pr(ctx, 3, merged=True)
    sent = []
    line = notify(ctx, lambda line, title: sent.append(line))
    assert line == ("Decision 2 · Try 1 · #1 Choose format please (spec-question: Which format?) · "
                    "#2 Unknown reason")
    assert sent == [line]
    assert len(line.splitlines()) == 1


@pytest.mark.parametrize("summary", [None, "", " \n\t "])
def test_notification_keeps_reason_without_missing_summary(ctx, monkeypatch, summary):
    flag(ctx)
    inbox = collect(ctx)
    inbox.items[0] = replace(inbox.items[0], summary=summary)
    monkeypatch.setattr("foragent.inspect_actions.collect", lambda ctx: inbox)
    sent = []
    assert notify(ctx, lambda line, title: sent.append(line)) == "Decision 1 · #1 Issue 1 (spec-question)"
    assert sent == ["Decision 1 · #1 Issue 1 (spec-question)"]


def test_notify_failure_does_not_mark_as_notified_and_can_retry(ctx):
    flag(ctx)
    def fail(*args):
        raise RuntimeError("notification unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        notify(ctx, fail)
    assert ctx.store.load_inspect().notified_signature is None
    calls = []
    assert notify(ctx, lambda *args: calls.append(args)) == "Decision 1 · #1 Issue 1 (spec-question: Summary 1)"
    assert len(calls) == 1


@pytest.mark.parametrize("fail_first", [False, True])
def test_concurrent_notifications_serialize_delivery_and_signature_and_release_after_failure(
    ctx, notification_process, fail_first,
):
    first = notification_process(ctx.store.root, pause_delivery=True, fail_delivery=fail_first)
    assert first.poll(10)
    assert first.recv() == ("delivering", "Decision 1 · #1 Issue 1 (spec-question: Summary 1)")
    assert (ctx.store.root / "notify.lock").exists()
    second = notification_process(ctx.store.root)
    try:
        assert not second.poll(0.2), "overlapping notification was not blocked"
        # A delivery holds only the notification lock: ticks and mark-seen still work.
        with ctx.store.tick_lock() as acquired:
            assert acquired
        assert mark_seen(ctx) == NOW.isoformat()
    finally:
        first.send("continue")
    assert first.poll(10)
    assert first.recv() == (("error", "delivery failed") if fail_first else ("result", "Decision 1 · #1 Issue 1 (spec-question: Summary 1)"))
    assert second.poll(10)
    if fail_first:
        assert second.recv() == ("delivering", "Decision 1 · #1 Issue 1 (spec-question: Summary 1)")
        assert second.poll(10)
    assert second.recv() == ("result", "Decision 1 · #1 Issue 1 (spec-question: Summary 1)" if fail_first else None)
    flag(ctx)
    assert ctx.store.load_inspect().notified_signature == pending_signature(collect(ctx))
    assert ctx.store.load_inspect().since == NOW.isoformat()


def test_macos_notifier_uses_escaped_script_without_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    macos_notifier('Review 1 · "quoted"', 'Foragent owner\\repo')
    assert calls == [(([
        "osascript", "-e", 'display notification "Review 1 · \\"quoted\\"" with title "Foragent owner\\\\repo"',
    ],), {"check": True, "capture_output": True, "text": True, "timeout": 10})]
