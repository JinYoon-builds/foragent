import json
import re

import pytest

from foragent.board_parents import order_parents, parent_state
from foragent.i18n import text
from foragent.tracker import GhTracker, Issue, ReferenceState


def child(number, labels=(), state="open", reason=None):
    return Issue(number, "", "", frozenset(labels), state, state_reason=reason)


@pytest.mark.parametrize(("children", "open_prs", "expected"), [
    ([], set(), None),
    ([child(1, ["needs-human"]), child(2, state="closed", reason="completed")], set(), "Needs human"),
    ([child(1, state="closed", reason="completed"), child(2, state="closed")], set(), "Done"),
    ([child(1, ["approved"])], {1}, "Running"),
    ([child(1, ["approved"]), child(2, state="closed", reason="not_planned")], set(), "Approved"),
    ([child(1, state="closed", reason="not_planned")], set(), "Done"),  # A cancelled child has ended.
    ([child(1, ["approved"]), child(2)], set(), "unapproved"),
])
def test_parent_state(children, open_prs, expected):
    assert parent_state(children, open_prs) == expected


def test_order_parents_keeps_waiting_parents_and_skips_approved_closed_and_childless():
    rows = [{"number": 1, "state": "open", "labels": [], "children": [2]},
            {"number": 3, "state": "open", "labels": ["approved"], "children": [4]},
            {"number": 5, "state": "open", "labels": ["blocked"], "children": [6]},
            {"number": 11, "state": "open", "labels": ["needs-human"], "children": [12]},
            {"number": 7, "state": "closed", "labels": [], "children": [8]},
            {"number": 9, "state": "open", "labels": [], "children": []}]
    assert order_parents(rows) == {1, 5, 11}


def test_gh_issue_keeps_state_reason():
    item = {"number": 4, "title": "t", "body": "", "labels": [], "state": "closed",
            "state_reason": "not_planned"}
    tracker = GhTracker("owner/repo", run=lambda argv, input=None: json.dumps(item))
    assert tracker.get_issue(4).state_reason == "not_planned"


def test_messages_have_both_languages():
    from foragent.board_parents import closing_comment

    assert "3" in text("order_record_closed", "en", count=3)
    assert "3" in text("order_record_closed", "ko", count=3)
    assert closing_comment("en", [], []) == text("order_record_closed", "en", count=0)
    for language, done, gone in (("en", "Completed:", "Cancelled"), ("ko", "완료:", "취소")):
        comment = closing_comment(language, [11, 12], [13])
        assert len(comment.splitlines()) == 1 and "3" in comment
        assert comment.index(done) < comment.index("#11") < comment.index("#12") < comment.index(gone)
        assert comment.index(gone) < comment.index("#13")
        assert gone not in closing_comment(language, [11], []) and done not in closing_comment(language, [], [13])
    assert "#9" in text("parent_child_not_planned", "ko", number=1, children="#9")
    closing = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+#\d+", re.I)
    lines = {language: text("batch_pr_parent", language, parent=99) for language in ("en", "ko")}
    assert lines["en"] != lines["ko"] and all("#99" in line for line in lines.values())
    assert not any(closing.search(line) for line in lines.values()), "Only the gate closes an order parent"


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("own_labels", [[], ["blocked"], ["needs-human"]])
def test_follow_children_closes_labeled_parent_and_reports_in_repo_language(
    monkeypatch, language, own_labels,
):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from foragent import board, board_parents

    flags, events, closed, synced = {}, [], [], []
    store = SimpleNamespace(
        local_issues=lambda: [{"number": 1, "state": "open", "labels": own_labels, "children": [2, 3, 4]}],
        load_local_flag=lambda n: flags.get(n),
        save_flag=lambda n, reason, flagged_at, local_only: flags.__setitem__(n, SimpleNamespace(reason=reason)),
    )
    children = {1: [child(2, state="closed", reason="completed"), child(3, state="closed", reason="not_planned"),
                    child(4)]}
    tracker = SimpleNamespace(sub_issues=lambda n: children[n],
                              close_issue=lambda n, comment: closed.append((n, comment)))
    ctx = SimpleNamespace(store=store, tracker=tracker, config=SimpleNamespace(language=language),
                          now=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(board_parents, "append_event", lambda *args: events.append(args))
    monkeypatch.setattr(board, "sync_status", lambda ctx, n, state, parent=False: synced.append((n, state, parent)))
    report = SimpleNamespace(actions=[])

    board_parents.follow_children(ctx, report)
    assert flags[1].reason == board_parents.NOT_PLANNED_FLAG and len(events) == 1
    assert closed == [] and synced == [(1, "Running", True)]

    children[1][2] = child(4, state="closed", reason="completed")  # The last open child ends.
    report.actions.clear()
    board_parents.follow_children(ctx, report)
    assert closed == [(1, board_parents.closing_comment(language, [2, 4], [3]))]
    assert synced[-1] == (1, "Done", True) and len(events) == 1, "Closing raises no new decision"
    assert report.actions == [text("parent_closed_report", language, number=1, completed=2, cancelled=1)]


def test_work_state_writes_skip_an_open_order_parent_card(monkeypatch):
    """Recovery, escalation and flags call sync_status; only the aggregation moves a parent."""
    from types import SimpleNamespace

    from foragent import board

    rows = [{"number": 1, "state": "open", "labels": ["blocked"], "children": [2]},
            {"number": 3, "state": "open", "labels": ["approved"], "children": [4]},
            {"number": 5, "state": "closed", "labels": [], "children": [6]},
            {"number": 7, "state": "open", "labels": [], "children": []}]
    written = []
    monkeypatch.setattr(board, "set_status", lambda issue, status: written.append((issue, status)))
    ctx = SimpleNamespace(tracker=SimpleNamespace(), repo="owner/repo",
                          config=SimpleNamespace(board="PVT_1", board_map=None),
                          store=SimpleNamespace(local_issues=lambda: rows))
    for number in (1, 3, 5, 7):
        board.sync_status(ctx, number, "Needs human")
    board.sync_status(ctx, 1, "Running", parent=True)
    assert written == [(3, "Needs human"), (5, "Needs human"), (7, "Needs human"), (1, "Running")]


@pytest.mark.parametrize("batch_rows", [
    pytest.param([{"number": 2, "pr_base": "foragent/batch-9", "pr_number": 50, "merged": True}],
                 id="child-merged-no-batch-pr"),
    pytest.param([{"number": 2, "pr_base": "foragent/batch-9", "pr_number": 50, "merged": True},
                  {"number": 9, "pr_head": "foragent/batch-9", "pr_number": 51, "merged": False}],
                 id="batch-pr-open"),
    pytest.param([], id="no-batch"),
])
def test_batch_parent_completes_when_its_children_are_done(monkeypatch, batch_rows):
    """Batch finalization continues for a closed parent, so completion never waits for it."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from foragent import board, board_parents

    rows = [{"number": 9, "state": "open", "labels": [], "children": [2]}]
    for row in batch_rows:
        target = next((r for r in rows if r["number"] == row["number"]), None)
        if target is None:
            rows.append({"state": "closed", "labels": [], **row})
        else:
            target.update(row)
    synced, closed = [], []
    tracker = SimpleNamespace(sub_issues=lambda n: [child(2, state="closed", reason="completed")],
                              close_issue=lambda n, comment: closed.append((n, comment)))
    ctx = SimpleNamespace(store=SimpleNamespace(local_issues=lambda: rows), tracker=tracker,
                          config=SimpleNamespace(language="en"),
                          now=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(board, "sync_status", lambda ctx, n, state, parent=False: synced.append((n, state)))
    report = SimpleNamespace(actions=[])
    board_parents.follow_children(ctx, report)
    assert closed == [(9, board_parents.closing_comment("en", [2], []))]
    assert synced == [(9, "Done")]



def _record_ctx(monkeypatch, rows, remote, children=None, root=None, clock=None):
    """An offline tick context whose local listing is ``rows`` and GitHub is ``remote``.

    Like a listing refresh, the rows never gain the issues a tick reads.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from foragent import board, board_parents

    flags, out = {}, SimpleNamespace(events=[], closed=[], synced=[], reads=[])

    def get_issue(n):
        out.reads.append(n)
        issue = remote[n]
        if isinstance(issue, Exception):
            raise issue
        return issue

    store = SimpleNamespace(
        root=root, local_issues=lambda: rows, load_local_flag=lambda n: flags.get(n),
        save_flag=lambda n, reason, flagged_at, local_only: flags.__setitem__(n, SimpleNamespace(reason=reason)),
    )
    def reference_states(numbers):
        # Like GitHub's one bulk read, any unreadable number fails the whole call.
        out.bulk_reads.append(list(numbers))
        return [ReferenceState(issue.number, issue.state, issue.state_reason, issue.is_pull_request)
                for issue in [ctx.tracker.get_issue(number) for number in numbers]]

    out.bulk_reads = []
    tracker = SimpleNamespace(sub_issues=lambda n: (children or {}).get(n, []), get_issue=get_issue,
                              reference_states=reference_states,
                              close_issue=lambda n, comment: out.closed.append((n, comment)))
    ctx = SimpleNamespace(store=store, tracker=tracker, config=SimpleNamespace(language="en"),
                          now=clock or (lambda: datetime(2026, 1, 1, tzinfo=UTC)))
    monkeypatch.setattr(board_parents, "append_event", lambda *args: out.events.append(args))
    monkeypatch.setattr(board, "sync_status",
                        lambda ctx, n, state, parent=False: out.synced.append((n, state, parent)))
    return ctx, out


def _record(number, mapping=None, edited=(), labels=(), children=()):
    acceptance = {str(n): {"tests/acceptance/test_x.py": "0" * 40} for n in edited}
    body = (f"Order.\n\n<!-- foragent:plan id=r-{number} map={json.dumps(mapping or {})} "
            f"acceptance={json.dumps(acceptance)} -->")
    return {"number": number, "state": "open", "labels": list(labels), "body": body,
            "children": list(children)}


def _row(number, state):
    return {"number": number, "state": state, "labels": [], "body": "", "title": ""}


def test_plan_references_reads_map_values_and_edited_issues():
    from foragent.board_parents import plan_references

    assert plan_references(_record(1, {"a": 2, "b": 3}, edited=[3, 4])["body"]) == {2, 3, 4}
    assert plan_references("no marker") == set()


def test_record_counts_distinct_references_and_reads_no_close_reasons(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_record(1, {"a": 2, "b": 3}, edited=[3, 4], children=[2]), _row(2, "closed"),
            _row(3, "closed"), _row(4, "closed")]
    ctx, out = _record_ctx(monkeypatch, rows, {}, children={1: [child(2, state="closed", reason="completed")]})
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [(1, board_parents.closing_comment("en", [2, 3, 4], []))], "Count distinct issues once"
    assert out.reads == [], "Closed references in the listing count as done without reading a reason"
    assert out.synced == [(1, "Done", True)], "A record it closes moves to Done"

    rows[2] = _row(3, "open")
    ctx, out = _record_ctx(monkeypatch, rows, {}, children={1: [child(2, state="closed", reason="completed")]})
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [] and out.reads == [], "Open referenced work keeps the record open without reads"
    assert out.synced == [(1, "Done", True)], "An open parent keeps today's column from its sub-issues"


def test_record_reads_missing_references_and_ignores_pull_requests(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    pr = Issue(3, "", "", frozenset(), "open", is_pull_request=True)
    rows = [_record(1, {"a": 2}, edited=[3, 4]), _row(2, "closed")]
    remote = {3: pr, 4: child(4, state="closed", reason="not_planned")}
    ctx, out = _record_ctx(monkeypatch, rows, remote)
    report = SimpleNamespace(actions=[])
    board_parents.follow_children(ctx, report)
    assert out.reads == [3, 4], "References missing from the listing are read once each"
    assert out.closed == [(1, board_parents.closing_comment("en", [2], [4]))], (
        "A pull request is ignored and a closed reference is done whatever its reason, named by it"
    )
    assert out.events == [], "Only native sub-issues raise the not_planned flag"

    remote[4] = child(4)
    ctx, out = _record_ctx(monkeypatch, rows, remote)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [] and out.synced == [], "An open missing reference keeps the record and its card"


def test_unreadable_reference_keeps_record_open_and_rate_limit_propagates(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents
    from foragent.tracker import TrackerError

    rows = [_record(1, edited=[2])]
    ctx, out = _record_ctx(monkeypatch, rows, {})

    def fail(n):
        raise TrackerError("gone")

    ctx.tracker.get_issue = fail
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == []

    def limited(n):
        raise TrackerError("API rate limit exceeded")

    ctx.tracker.get_issue = limited
    with pytest.raises(TrackerError):
        board_parents.follow_children(ctx, SimpleNamespace(actions=[]))


def test_every_unlisted_reference_is_read_fresh_in_one_tick_without_a_cap(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from foragent import board_parents

    edited = list(range(100, 125))
    rows = [_record(1, edited=edited), _record(2, edited=[100])]
    ctx, out = _record_ctx(monkeypatch, rows, {n: child(n, state="closed") for n in edited}, root=tmp_path)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert sorted(out.reads) == sorted([*edited, 100]), "Each record reads its unlisted references fresh"
    assert sorted(out.closed) == [(1, board_parents.closing_comment("en", edited, [])),
                                  (2, board_parents.closing_comment("en", [100], []))]
    assert list(tmp_path.iterdir()) == [], "Nothing about references is stored between ticks"


def test_reference_read_closed_earlier_is_read_again_on_every_tick(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents
    from foragent.tracker import TrackerError

    rows = [_record(1, edited=[4, 5, 6])]
    remote = {4: Issue(4, "", "", frozenset(), "open", is_pull_request=True),
              5: child(5, state="closed"), 6: child(6)}
    ctx, out = _record_ctx(monkeypatch, rows, remote)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.bulk_reads == [[4, 5, 6]] and out.closed == []
    remote[5], remote[6] = child(5), child(6, state="closed")  # #5 reopened; #6 closed.
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.bulk_reads[1:] == [[4, 5, 6]] and out.closed == [], "A reopened reference keeps the record open"
    remote[5] = TrackerError("Not Found")
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.bulk_reads[2:] == [[4, 5, 6]] and out.closed == [], "An unreadable reference keeps the record open"
    remote[5] = child(5, state="closed")
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.bulk_reads[3:] == [[4, 5, 6]]
    assert out.closed == [(1, board_parents.closing_comment("en", [5, 6], []))], "The pull request is ignored"


def test_unparseable_marker_is_skipped_quietly_even_with_completed_children(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    for body in ("Order.\n\n<!-- foragent:plan id=r-1 map={broken -->",
                 'Order.\n\n<!-- foragent:plan id=r-1 map={"a": "x"} acceptance={} -->'):
        rows = [{**_record(1, children=[2]), "body": body}, _row(2, "closed")]
        ctx, out = _record_ctx(monkeypatch, rows, {},
                               children={1: [child(2, state="closed", reason="completed")]})
        report = SimpleNamespace(actions=[])
        board_parents.follow_children(ctx, report)
        assert out.closed == [] and out.reads == [] and report.actions == []
        assert out.synced == [(1, "Done", True)], "An open parent keeps today's column"
    assert board_parents.plan_references(rows[0]["body"]) is None


def test_record_referencing_itself_stays_open(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_record(1, {"a": 1, "b": 2}, children=[2]), _row(2, "closed")]
    ctx, out = _record_ctx(monkeypatch, rows, {},
                           children={1: [child(2, state="closed", reason="completed")]})
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [], "The record itself is open work it references"
    assert board_parents.plan_references(rows[0]["body"]) == {1, 2}


def test_not_planned_sub_issue_flags_even_while_a_reference_is_open(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_record(1, {"a": 2}, edited=[3], children=[2]), {**_row(2, "closed"), "state_reason": "not_planned"},
            _row(3, "open")]
    ctx, out = _record_ctx(monkeypatch, rows, {})
    report = SimpleNamespace(actions=[])
    board_parents.follow_children(ctx, report)
    assert report.actions == [text("parent_child_not_planned", "en", number=1, children="#2")]
    assert out.closed == []
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert len(out.events) == 1, "The flag is raised once"


def test_record_with_a_lane_label_or_approval_is_not_closed_by_references(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    for labels in (["lane:main"], ["approved"]):
        rows = [_record(1, {"a": 2}, labels=labels), _row(2, "closed")]
        ctx, out = _record_ctx(monkeypatch, rows, {2: child(2, state="closed", reason="completed")})
        board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
        assert out.closed == [] and out.reads == []


def test_known_pull_request_reference_spends_no_read(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_record(1, {"a": 2, "b": 7}), {**_row(2, "closed"), "pr_number": 7, "merged": True}]
    ctx, out = _record_ctx(monkeypatch, rows, {})
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.reads == [], "A pull request the listing already shows is ignored without a read"
    assert out.closed == [(1, board_parents.closing_comment("en", [2], []))]


def test_order_whose_last_child_was_cancelled_closes_without_a_decision_flag(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_record(1, {"a": 2, "b": 3}, children=[2, 3]), _row(2, "closed"),
            {**_row(3, "closed"), "state_reason": "not_planned"}]
    ctx, out = _record_ctx(monkeypatch, rows, {})
    report = SimpleNamespace(actions=[])
    board_parents.follow_children(ctx, report)
    assert out.closed == [(1, board_parents.closing_comment("en", [2], [3]))]
    assert out.events == [] and ctx.store.load_local_flag(1) is None, "A closed order needs no decision"
    assert report.actions == [text("parent_closed_report", "en", number=1, completed=1, cancelled=1)]
    assert out.synced == [(1, "Done", True)]


def _parent_row(number, children, summary=None, **fields):
    observed = {"sub_issues": summary} if summary else {}
    return {"number": number, "state": "open", "labels": [], "body": "", "children": children,
            "observed": observed, **fields}


def _counting_ctx(monkeypatch, rows, remote=None):
    """Offline context that counts sub-issue reads; any listed fact must make them unnecessary."""
    ctx, out = _record_ctx(monkeypatch, rows, {})
    out.child_reads = []

    def sub_issues(n):
        out.child_reads.append(n)
        return (remote or {}).get(n, [])

    ctx.tracker.sub_issues = sub_issues
    return ctx, out


def test_listed_child_facts_decide_without_a_read_and_keep_closing_groups(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_parent_row(1, [2, 3], {"total": 2, "completed": 1}), _row(2, "closed"), _row(3, "open"),
            _parent_row(4, [5, 6], {"total": 2, "completed": 2}), _row(5, "closed"),
            {**_row(6, "closed"), "state_reason": "not_planned"}]
    ctx, out = _counting_ctx(monkeypatch, rows)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.child_reads == [], "The listing already shows every child"
    assert out.closed == [(4, board_parents.closing_comment("en", [5], [6]))], "Only the ended parent closes"
    assert sorted(out.synced) == [(1, "Running", True), (4, "Done", True)]


def test_child_only_change_closes_a_parent_whose_own_row_is_unchanged(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_parent_row(1, [2], {"total": 1, "completed": 1}), _row(2, "open")]
    ctx, out = _counting_ctx(monkeypatch, rows)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == []
    rows[1] = _row(2, "closed")  # The summary was already full; only the child's row tells.
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [(1, board_parents.closing_comment("en", [2], []))] and out.child_reads == []


@pytest.mark.parametrize(("summary", "reads", "closed"), [
    pytest.param({"total": 2, "completed": 1}, [], False, id="open-work-keeps-it-open-without-a-read"),
    pytest.param({"total": 2, "completed": 2}, [1], True, id="full-summary-is-checked-by-a-read"),
    pytest.param(None, [1], True, id="no-summary-is-no-evidence"),
    pytest.param({"total": 3, "completed": 3}, [1], True, id="total-disagrees-with-known-children"),
])
def test_summary_never_closes_by_itself_when_a_child_has_no_row(monkeypatch, summary, reads, closed):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_parent_row(1, [2, 3], summary), _row(2, "closed")]
    remote = {1: [child(2, state="closed"), child(3, state="closed")]}
    ctx, out = _counting_ctx(monkeypatch, rows, remote)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.child_reads == reads
    assert bool(out.closed) is closed
    if not closed:
        assert out.synced == [(1, "Running", True)], "The unlisted open child counts as open work"


def test_summary_total_above_known_children_forces_a_read_before_closing(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_parent_row(1, [2], {"total": 2, "completed": 1}), _row(2, "closed")]
    ctx, out = _counting_ctx(monkeypatch, rows, {1: [child(2, state="closed"), child(3)]})
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.child_reads == [1] and out.closed == [], "A child added since the last edge read is open work"


def test_read_showing_open_work_keeps_a_full_summary_parent_open(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_parent_row(1, [2], {"total": 1, "completed": 1})]
    ctx, out = _counting_ctx(monkeypatch, rows, {1: [child(2)]})
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.child_reads == [1] and out.closed == [], "Any open work keeps the parent open"


def test_closes_come_before_reads_and_one_failure_defers_only_its_parent(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents
    from foragent.tracker import TrackerError

    rows = [_parent_row(1, [10]), _parent_row(2, [20]), _parent_row(3, [30]), _row(30, "closed")]
    ctx, out = _counting_ctx(monkeypatch, rows)
    seen = []

    def sub_issues(n):
        seen.append(("read", n))
        raise TrackerError("gh call timed out")

    def close_issue(n, comment):
        seen.append(("close", n))

    ctx.tracker.sub_issues, ctx.tracker.close_issue = sub_issues, close_issue
    with pytest.raises(TrackerError, match="timed out"):
        board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert seen[0] == ("close", 3), "A parent ready to close does not wait behind reads"
    assert sorted(seen[1:]) == [("read", 1), ("read", 2)], "A failed read does not stop the other parents"


def test_rate_limit_stops_the_step_at_once(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents
    from foragent.tracker import TrackerError

    rows = [_parent_row(1, [10]), _parent_row(2, [20]), _row(10, "closed"), _row(20, "closed")]
    ctx, out = _counting_ctx(monkeypatch, rows)
    closes = []

    def close_issue(n, comment):
        closes.append(n)
        raise TrackerError("API rate limit exceeded")

    ctx.tracker.close_issue = close_issue
    with pytest.raises(TrackerError, match="rate limit"):
        board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert closes == [1], "Lowest number first, and the rate limit reaches the tick immediately"


def test_a_record_read_on_every_tick_cannot_starve_a_later_one(monkeypatch):
    """Lower records reread an open unlisted reference forever, and only one read fits a tick."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from foragent import board_parents
    from foragent.tracker import TrackerError

    rows = [_record(number, edited=[number * 100]) for number in range(1, 7)]
    remote = {number * 100: child(number * 100) for number in range(1, 6)}
    remote[600] = child(600, state="closed")
    moment = [datetime(2026, 1, 1, tzinfo=UTC)]
    ctx, out = _record_ctx(monkeypatch, rows, remote, clock=lambda: moment[0])
    original = ctx.tracker.reference_states
    budget = [0]

    def reference_states(numbers):
        if budget[0] == 0:
            raise TrackerError("gh time budget exhausted")
        budget[0] -= 1
        return original(numbers)

    ctx.tracker.reference_states = reference_states
    for tick in range(60):
        budget[0] = 1
        moment[0] += timedelta(minutes=5)
        board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
        if out.closed:
            break
    assert out.closed == [(6, board_parents.closing_comment("en", [600], []))], (
        "The highest record must be reached within a finite number of ticks"
    )


def _four_second_calls(monkeypatch, tracker, names):
    """The real shared budget, with each named tracker call taking four seconds of a fake clock."""
    from foragent import gh
    from foragent.tracker import TrackerError

    clock, calls = [0.0], []
    monkeypatch.setattr(gh.time, "monotonic", lambda: clock[0])
    for name in names:
        original = getattr(tracker, name)

        def timed(*args, _name=name, _original=original, **kwargs):
            try:
                available = gh.call_timeout(float("inf"))
            except gh.DeadlineExhausted as exc:
                raise TrackerError(str(exc)) from exc
            clock[0] += min(4.0, available)
            if available < 4.0:
                raise TrackerError("timed out at its deadline")
            calls.append(_name)
            return _original(*args, **kwargs)

        setattr(tracker, name, timed)
    return calls


def _bounded_tick(ctx):
    from types import SimpleNamespace

    from foragent import board_parents, gh
    from foragent.tracker import TrackerError

    try:
        with gh.Budget(10, 5).spend():
            board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    except TrackerError as exc:
        assert not exc.is_rate_limit


def test_ended_record_with_many_unlisted_references_closes_under_the_tick_budget(monkeypatch):
    """Separate reads spend the budget and start over each tick, so this record would never close."""
    from foragent import board_parents

    references = list(range(100, 130))
    rows = [_record(1, edited=references)]
    remote = {n: child(n, state="closed", reason="not_planned" if n == 100 else "completed") for n in references}
    ctx, out = _record_ctx(monkeypatch, rows, remote)
    calls = _four_second_calls(monkeypatch, ctx.tracker, ["reference_states", "close_issue"])
    _bounded_tick(ctx)
    assert calls == ["reference_states", "close_issue"], "One read for every reference, then the close"
    assert out.closed == [(1, board_parents.closing_comment("en", references[1:], [100]))]


def test_unlisted_child_read_then_close_fits_the_tick_budget(monkeypatch):
    from foragent import board_parents

    rows = [_parent_row(1, [2], {"total": 1, "completed": 1})]
    ctx, out = _counting_ctx(monkeypatch, rows, {1: [child(2, state="closed")]})
    calls = _four_second_calls(monkeypatch, ctx.tracker, ["sub_issues", "close_issue"])
    _bounded_tick(ctx)
    assert calls == ["sub_issues", "close_issue"]
    assert out.closed == [(1, board_parents.closing_comment("en", [2], []))]


def test_gh_reference_states_reads_every_number_in_one_call():
    from foragent.tracker import ReferenceState, TrackerError

    calls = []

    def run(argv, input=None):
        calls.append(json.loads(input))
        return json.dumps({"data": {"repository": {
            "i4": {"number": 4, "state": "OPEN", "__typename": "PullRequest"},
            "i5": {"number": 5, "state": "CLOSED", "stateReason": "NOT_PLANNED", "__typename": "Issue"},
            "i6": {"number": 6, "state": "CLOSED", "stateReason": None, "__typename": "Issue"},
        }}})

    tracker = GhTracker("owner/repo", run=run)
    assert tracker.reference_states([6, 5, 4, 5]) == [
        ReferenceState(4, "open", None, True), ReferenceState(5, "closed", "not_planned"),
        ReferenceState(6, "closed"),
    ]
    assert len(calls) == 1 and calls[0]["variables"] == {"owner": "owner", "name": "repo"}
    assert all(f"number: {n})" in calls[0]["query"] for n in (4, 5, 6))
    assert tracker.reference_states([]) == [] and len(calls) == 1, "Nothing to read costs no call"

    missing = GhTracker("owner/repo", run=lambda argv, input=None: json.dumps(
        {"data": {"repository": {"i9": None}}, "errors": [{"type": "NOT_FOUND"}]}))
    with pytest.raises(TrackerError):
        missing.reference_states([9])
