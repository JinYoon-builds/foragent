import json
import re

import pytest

from foragent.board_parents import order_parents, parent_state
from foragent.i18n import text
from foragent.tracker import GhTracker, Issue


def child(number, labels=(), state="open", reason=None):
    return Issue(number, "", "", frozenset(labels), state, state_reason=reason)


@pytest.mark.parametrize(("children", "open_prs", "expected"), [
    ([], set(), None),
    ([child(1, ["needs-human"]), child(2, state="closed", reason="completed")], set(), "Needs human"),
    ([child(1, state="closed", reason="completed"), child(2, state="closed")], set(), "Done"),
    ([child(1, ["approved"])], {1}, "Running"),
    ([child(1, ["approved"]), child(2, state="closed", reason="not_planned")], set(), "Approved"),
    ([child(1, state="closed", reason="not_planned")], set(), "unapproved"),
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
    assert "3" in text("order_record_closed", "en", count=3)
    assert "3" in text("order_record_closed", "ko", count=3)
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
        local_issues=lambda: [{"number": 1, "state": "open", "labels": own_labels, "children": [2, 3]}],
        load_flag=lambda n: flags.get(n),
        save_flag=lambda n, reason, flagged_at: flags.__setitem__(n, SimpleNamespace(reason=reason)),
    )
    children = {1: [child(2, state="closed", reason="completed"), child(3, state="closed", reason="not_planned")]}
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

    children[1][1] = child(3, state="closed", reason="completed")
    report.actions.clear()
    board_parents.follow_children(ctx, report)
    assert closed == [(1, text("order_record_closed", language, count=2))]
    assert synced[-1] == (1, "Done", True)
    assert report.actions == [text("parent_closed_report", language, number=1, count=2)]


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
    assert closed == [(9, text("order_record_closed", "en", count=1))]
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
        root=root, local_issues=lambda: rows, load_flag=lambda n: flags.get(n),
        save_flag=lambda n, reason, flagged_at: flags.__setitem__(n, SimpleNamespace(reason=reason)),
    )
    tracker = SimpleNamespace(sub_issues=lambda n: (children or {}).get(n, []), get_issue=get_issue,
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
    assert out.closed == [(1, text("order_record_closed", "en", count=3))], "Count distinct issues once"
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
    assert out.closed == [(1, text("order_record_closed", "en", count=2))], (
        "A pull request is ignored and a closed reference is done whatever its reason"
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


def test_reference_reads_stop_at_the_tick_allowance_and_resume_next_tick(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from foragent import board_parents

    cap = board_parents.MAX_REFERENCE_READS
    first = list(range(100, 100 + cap - 1))
    rows = [_record(1, edited=first), _record(2, edited=[200, 201]), _record(3, edited=[300])]
    remote = {n: child(n, state="closed") for n in [*first, 200, 201, 300]}
    ctx, out = _record_ctx(monkeypatch, rows, remote, root=tmp_path)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert len(out.reads) == cap, "One tick reads at most the allowance"
    assert [n for n, _ in out.closed] == [1], "Records past the allowance wait for the next tick"
    rows[0]["state"] = "closed"
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.reads[cap:] == [201, 300], "The next tick reads only what is still unconfirmed"
    assert [n for n, _ in out.closed] == [1, 2, 3]


def test_record_with_more_unlisted_issues_than_one_tick_reads_closes_next_tick(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from foragent import board_parents

    cap = board_parents.MAX_REFERENCE_READS
    edited = list(range(100, 100 + cap + 1))
    rows = [_record(1, edited=edited)]
    ctx, out = _record_ctx(monkeypatch, rows, {n: child(n, state="closed") for n in edited},
                           root=tmp_path)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [] and len(out.reads) == cap
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [(1, text("order_record_closed", "en", count=cap + 1))]
    assert sorted(out.reads) == edited, "Every reference is read exactly once"


def test_confirmed_reads_expire_and_open_work_clears_them(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from foragent import board_parents

    cap = board_parents.MAX_REFERENCE_READS
    edited = list(range(100, 100 + cap + 1))
    remote = {n: child(n, state="closed") for n in edited}
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    rows = [_record(1, edited=edited)]
    ctx, out = _record_ctx(monkeypatch, rows, remote, root=tmp_path, clock=lambda: now[0])
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    now[0] += board_parents.CONFIRMED_FOR + timedelta(seconds=1)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [] and out.reads[cap:] == edited[:cap], "Expired reads are read again"

    rows.append(_row(edited[-1], "open"))  # The one unread reference shows up open in the listing.
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    rows.pop()
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [] and out.reads[2 * cap:] == edited[:cap], (
        "Open work clears earlier closed reads, so they are read again"
    )


def test_reference_shared_by_records_is_read_once_per_tick(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_record(1, edited=[5]), _record(2, {"a": 5})]
    ctx, out = _record_ctx(monkeypatch, rows, {5: child(5, state="closed")})
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.reads == [5]
    assert [n for n, _ in out.closed] == [1, 2]


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

    rows = [_record(1, {"a": 2}, edited=[3], children=[2]), _row(2, "closed"), _row(3, "open")]
    ctx, out = _record_ctx(monkeypatch, rows, {},
                           children={1: [child(2, state="closed", reason="not_planned")]})
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
    assert out.closed == [(1, text("order_record_closed", "en", count=1))]


def test_record_with_more_unlisted_pull_requests_than_one_tick_allows_still_closes(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from foragent import board_parents

    cap = board_parents.MAX_REFERENCE_READS
    prs = list(range(100, 100 + cap + 1))
    rows = [_record(1, edited=[*prs, 500])]
    remote = {n: Issue(n, "", "", frozenset(), "open", is_pull_request=True) for n in prs}
    remote[500] = child(500, state="closed")
    ctx, out = _record_ctx(monkeypatch, rows, remote, root=tmp_path)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [] and out.reads == prs[:cap]
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.reads[cap:] == [prs[-1], 500], "Pull requests read on an earlier tick are not read again"
    assert out.closed == [(1, text("order_record_closed", "en", count=1))]

    ctx, out = _record_ctx(monkeypatch, rows, remote, root=tmp_path)
    rows[0]["state"] = "open"
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.reads == [], "A new tick context reads the remembered reads from the state folder"


def test_unreadable_references_do_not_starve_later_records(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from foragent import board_parents
    from foragent.tracker import TrackerError

    cap = board_parents.MAX_REFERENCE_READS
    rows = [_record(n, edited=[1000 + n]) for n in range(1, cap + 1)] + [_record(cap + 1, edited=[2000])]
    remote = {1000 + n: TrackerError("Not Found") for n in range(1, cap + 1)}
    remote[2000] = child(2000, state="closed")
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    ctx, out = _record_ctx(monkeypatch, rows, remote, clock=lambda: now[0])
    for _ in range(cap + 1):
        board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
        assert len(out.reads) <= cap * (_ + 1)
        now[0] += timedelta(seconds=board_parents.ROTATION_SECONDS)
    assert {n for n, _ in out.closed} == {cap + 1}, (
        "Records that cannot be read must not take the whole allowance on every tick"
    )


def test_closed_read_is_confirmed_again_in_the_closing_tick(monkeypatch, tmp_path):
    import json as _json
    from types import SimpleNamespace

    from foragent import board_parents
    from foragent.tracker import TrackerError

    rows = [_record(1, edited=[4, 5, 6])]
    remote = {4: Issue(4, "", "", frozenset(), "open", is_pull_request=True),
              5: child(5, state="closed"), 6: child(6)}
    ctx, out = _record_ctx(monkeypatch, rows, remote, root=tmp_path)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.reads == [4, 5, 6] and out.closed == []
    remote[5] = TrackerError("Not Found")  # Unreadable now, after an earlier read saw it closed.
    remote[6] = child(6, state="closed")
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.reads[3:] == [5] and out.closed == [], "An unreadable reference keeps the record open"
    remote[5] = child(5, state="closed")
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.reads[4:] == [5, 6], "The pull request read earlier is not read again"
    assert out.closed == [(1, text("order_record_closed", "en", count=2))]

    path = tmp_path / board_parents.REFERENCE_READS_FILE
    assert _json.loads(path.read_text())["prs"] == [4]
    rows[0]["state"] = "closed"
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert _json.loads(path.read_text()) == {"prs": [], "confirmed": {}}, (
        "Reads for records that are no longer open are dropped"
    )


def test_an_unfinished_sub_issue_clears_earlier_closed_reads(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from foragent import board_parents

    cap = board_parents.MAX_REFERENCE_READS
    edited = list(range(100, 100 + cap + 1))
    rows = [_record(1, edited=edited, children=[2])]
    subs = {1: [child(2, state="closed", reason="completed")]}
    ctx, out = _record_ctx(monkeypatch, rows, {n: child(n, state="closed") for n in edited},
                           children=subs, root=tmp_path)
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    subs[1] = [child(2)]  # Reopened sub-issue: the record has open work again.
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    subs[1] = [child(2, state="closed", reason="completed")]
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [] and out.reads[cap:] == edited[:cap], (
        "Closed reads taken before the sub-issue reopened no longer count"
    )
