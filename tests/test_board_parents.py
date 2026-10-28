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
                          config=SimpleNamespace(language="en"))
    monkeypatch.setattr(board, "sync_status", lambda ctx, n, state, parent=False: synced.append((n, state)))
    report = SimpleNamespace(actions=[])
    board_parents.follow_children(ctx, report)
    assert closed == [(9, text("order_record_closed", "en", count=1))]
    assert synced == [(9, "Done")]



def _record_ctx(monkeypatch, rows, remote, children=None):
    """An offline tick context whose local listing is ``rows`` and GitHub is ``remote``."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from foragent import board, board_parents

    flags, out = {}, SimpleNamespace(events=[], closed=[], synced=[], reads=[])

    def get_issue(n):
        out.reads.append(n)
        return remote[n]

    store = SimpleNamespace(
        local_issues=lambda: rows, load_flag=lambda n: flags.get(n),
        save_flag=lambda n, reason, flagged_at: flags.__setitem__(n, SimpleNamespace(reason=reason)),
    )
    tracker = SimpleNamespace(sub_issues=lambda n: (children or {}).get(n, []), get_issue=get_issue,
                              close_issue=lambda n, comment: out.closed.append((n, comment)))
    ctx = SimpleNamespace(store=store, tracker=tracker, config=SimpleNamespace(language="en"),
                          now=lambda: datetime(2026, 1, 1, tzinfo=UTC))
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


def test_record_counts_distinct_references_and_reads_reasons_only_when_all_closed(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_record(1, {"a": 2, "b": 3}, edited=[3], children=[2]), _row(2, "closed"), _row(3, "closed")]
    ctx, out = _record_ctx(monkeypatch, rows, {3: child(3, state="closed", reason="completed")},
                           children={1: [child(2, state="closed", reason="completed")]})
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [(1, text("order_record_closed", "en", count=2))]
    assert out.reads == [3], "The sub-issue already carries its reason; only the other reference is read"
    assert out.synced == [(1, "Done", True)]

    rows[2] = _row(3, "open")
    ctx, out = _record_ctx(monkeypatch, rows, {}, children={1: [child(2, state="closed", reason="completed")]})
    board_parents.follow_children(ctx, SimpleNamespace(actions=[]))
    assert out.closed == [] and out.reads == [], "Open referenced work keeps the record open without reads"


def test_record_reads_missing_reference_and_flags_not_planned(monkeypatch):
    from types import SimpleNamespace

    from foragent import board_parents

    rows = [_record(1, {"a": 2}, edited=[3]), _row(2, "closed")]
    remote = {2: child(2, state="closed", reason="completed"), 3: child(3, state="closed", reason="not_planned")}
    ctx, out = _record_ctx(monkeypatch, rows, remote)
    report = SimpleNamespace(actions=[])
    board_parents.follow_children(ctx, report)
    assert sorted(out.reads) == [2, 3], "A reference missing from the listing is read from GitHub"
    assert out.closed == [], "A not_planned reference keeps the record open"
    assert [event[3] for event in out.events] == [board_parents.NOT_PLANNED_FLAG]
    assert report.actions == [text("parent_child_not_planned", "en", number=1, children="#3")]
    assert out.synced == [], "A record without sub-issues keeps today's board column"

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
