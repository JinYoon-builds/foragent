"""#837 with the real StateStore and RecordingTracker across listing refreshes."""

import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_order_record_refs import add_record, add_work, tick_again
from test_parent_follows_children import (
    BACKLOG, DONE, IN_PROGRESS, add_parent, card_column, gate_tick, offline_only, parent_case,  # noqa: F401
    seed_card,
)
from test_skills_autosync import isolated_home  # noqa: F401

import pytest

from foragent.scheduler import tick
from foragent.tracker import TrackerError
from test_orch_review_handoff import tick_context

FULL = (("total", 1), ("completed", 1), ("percent_completed", 100))


def test_unlisted_reference_read_closed_earlier_is_read_again_before_closing(parent_case):  # noqa: F811
    case = parent_case
    add_work(case, 5, closed=True)
    add_work(case, 6)
    add_record(case, 40, edited=[5, 6])
    # Both are outside the listing, so the tick reads them: #5 closed, #6 open.
    listing, recent = case.tracker.list_issues, case.tracker.issues_closed_since
    case.tracker.list_issues = lambda *a, **k: [i for i in listing(*a, **k) if i.number not in {5, 6}]
    case.tracker.issues_closed_since = lambda *a, **k: [i for i in recent(*a, **k) if i.number not in {5, 6}]
    real, unreadable, reads = case.tracker.get_issue, [], []

    def get_issue(n):
        reads.append(n)
        if n in unreadable:
            raise TrackerError(f"Could not resolve to an issue with the number of {n}. (Not Found)")
        return real(n)

    case.tracker.get_issue = get_issue
    gate_tick(case)
    assert case.tracker.get_issue(40).state == "open"
    assert {5, 6} <= set(reads), "The tick read #5 closed and #6 open"
    unreadable.append(5)
    case.tracker.close_issue(6)
    tick_again(case)
    assert case.tracker.get_issue(40).state == "open", "A reference that cannot be read keeps the record open"
    unreadable.clear()
    tick_again(case)
    assert case.tracker.get_issue(40).state == "closed"
    assert [entry for entry in case.tracker.closures if entry[0] == 40] == [(40, "completed")]


def test_record_with_many_unlisted_closed_references_closes_in_one_tick(parent_case):  # noqa: F811
    case = parent_case
    edited = list(range(100, 125))
    for number in edited:
        add_work(case, number, closed=True)
    add_record(case, 40, edited=edited)
    hidden = set(edited)
    listing, recent = case.tracker.list_issues, case.tracker.issues_closed_since
    case.tracker.list_issues = lambda *a, **k: [i for i in listing(*a, **k) if i.number not in hidden]
    case.tracker.issues_closed_since = lambda *a, **k: [i for i in recent(*a, **k) if i.number not in hidden]
    real, reads = case.tracker.get_issue, []

    def get_issue(n):
        if n in hidden:
            reads.append(n)
        return real(n)

    case.tracker.get_issue = get_issue
    gate_tick(case)
    assert case.tracker.get_issue(40).state == "closed", "Every unlisted reference is read in the same tick"
    assert sorted(reads) == edited, "Each unlisted reference is read exactly once"


def _slow_reads_before_parents(case, monkeypatch):
    """The locked four-second transport, with the reads earlier tick steps make costing time too."""
    import test_parent_sync_starve as starve

    monkeypatch.setattr(starve, "PER_ISSUE", (*starve.PER_ISSUE, "issues_closed_since", "find_pr", "reference_states"))
    case.config = case.ctx.config = replace(case.config, language="en")
    return starve.Latency(case.tracker, monkeypatch)


def test_ended_record_with_an_unlisted_pull_request_closes_though_batch_reads_share_the_budget(
        parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    gate_tick(case)  # Establish the watermark: the batch step's closed read now runs on every tick.
    add_work(case, 5, closed=True)
    case.tracker.add_issue(7, "A pull request", is_pull_request=True)
    add_record(case, 40, edited=[5, 7])
    latency = _slow_reads_before_parents(case, monkeypatch)
    for _ in range(6):
        latency.gate_tick(case)
        if case.tracker.issues[40].state == "closed":
            break
    assert case.tracker.issues[40].state == "closed", f"The record never closed: {latency.calls}"
    assert sum(number == 40 for number, _ in case.tracker.closures) == 1
    assert len(case.tracker.comments[40]) == 1


def test_parent_card_moves_though_batch_reads_share_the_budget(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    children = add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    assert card_column(case.tracker, case.board_id, 10) == BACKLOG
    case.tracker.close_issue(children[0])
    latency = _slow_reads_before_parents(case, monkeypatch)
    latency.gate_tick(case)
    assert card_column(case.tracker, case.board_id, 10) == IN_PROGRESS, f"The move restarted: {latency.calls}"
    assert case.tracker.issues[10].state == "open", "An open child keeps the parent open"


def test_batch_step_still_reads_after_parents_spend_the_budget(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    finished = [add_parent(case, ["completed"], number=number) for number in (10, 20, 30)]
    assert finished
    latency = _slow_reads_before_parents(case, monkeypatch)
    latency.gate_tick(case)  # Closing and its card write use the budget; the closed read has to wait.
    for _ in range(6):
        latency.gate_tick(case)
    assert all(case.tracker.issues[number].state == "closed" for number in (10, 20, 30))
    assert any(name == "issues_closed_since" and bound for name, _, bound, _ in latency.calls), (
        "Once parents are done, the batch step gets its turn again"
    )
    assert case.store.closed_issue_watermark() is not None


def _frozen_parent_listing(case, monkeypatch, parent, summary, *, hidden=()):
    """A listing whose parent facts stay put, as after a race between the listing and its edge read."""
    original = case.tracker.list_issues

    def listing(state="all"):
        return [replace(issue, updated_at="2026-01-01T00:00:00+00:00", sub_issues_summary=summary)
                if issue.number == parent else issue
                for issue in original(state) if issue.number not in hidden]

    monkeypatch.setattr(case.tracker, "list_issues", listing)
    recent = case.tracker.issues_closed_since
    monkeypatch.setattr(case.tracker, "issues_closed_since",
                        lambda since: [issue for issue in recent(since) if issue.number not in hidden])


def _stale_edge(case, monkeypatch, number, *, edited=(), completed=1):
    """The refresh's edge read misses the second child; later reads see both, and nothing changes the listing."""
    add_work(case, 5)
    add_work(case, 6)
    add_record(case, number, mapping={"a": 5, "b": 6}, edited=edited)
    case.tracker.add_sub_issue(number, 5)
    case.tracker.add_sub_issue(number, 6)
    summary = (("total", 2), ("completed", completed), ("percent_completed", 50 * completed))
    _frozen_parent_listing(case, monkeypatch, number, summary)
    edges, first = case.tracker.sub_issues, [True]

    def racing(n):
        read = edges(n)
        if n == number and first:
            first.clear()
            return [issue for issue in read if issue.number == 5]
        return read

    monkeypatch.setattr(case.tracker, "sub_issues", racing)


def test_stale_edges_read_then_reference_read_then_close_continues_on_the_next_tick(
        parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    case.tracker.add_issue(7, "A pull request", is_pull_request=True)
    _stale_edge(case, monkeypatch, 40, edited=[7], completed=2)
    case.tracker.issues[5] = replace(case.tracker.issues[5], state="closed", state_reason="completed")
    case.tracker.issues[6] = replace(case.tracker.issues[6], state="closed", state_reason="not_planned")
    latency = _slow_reads_before_parents(case, monkeypatch)
    for _ in range(6):
        latency.gate_tick(case)
        if case.tracker.issues[40].state == "closed":
            break
    assert case.tracker.issues[40].state == "closed", f"The sequence restarted: {latency.calls}"
    assert not any(name == "sub_issues" for name, *_ in latency.calls), "The second tick continues past the read"
    assert len(case.tracker.comments[40]) == 1
    assert "#5" in case.tracker.comments[40][0].body and "#6" in case.tracker.comments[40][0].body


def test_stale_edges_read_then_card_move_continues_on_the_next_tick(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    _stale_edge(case, monkeypatch, 40)
    case.tracker.issues[5] = replace(case.tracker.issues[5], state="closed", state_reason="completed")
    latency = _slow_reads_before_parents(case, monkeypatch)
    for _ in range(6):
        latency.gate_tick(case)
        if card_column(case.tracker, case.board_id, 40) == IN_PROGRESS:
            break
    assert card_column(case.tracker, case.board_id, 40) == IN_PROGRESS, f"The sequence restarted: {latency.calls}"
    latency.gate_tick(case)
    assert [call for call in latency.calls if call[0] != "issues_closed_since"] == [], (
        "Once repaired, an unchanged parent costs no calls"
    )


def _unlisted_child_after_a_failed_close(case, monkeypatch, summary):
    """The refresh that reads a new edge shows the child once; the close fails, then the row is gone."""
    add_work(case, 5, closed=True)
    case.tracker.add_issue(7, "A pull request", is_pull_request=True)
    add_record(case, 40, mapping={"a": 5}, edited=[7])
    case.tracker.add_sub_issue(40, 5)
    _frozen_parent_listing(case, monkeypatch, 40, summary, hidden={5})
    close, failures = case.tracker.close_issue, [1]

    def fails_once(number, comment=None):
        if failures:
            failures.pop()
            raise TrackerError("gh call failed")
        return close(number, comment)

    monkeypatch.setattr(case.tracker, "close_issue", fails_once)
    case.config = case.ctx.config = replace(case.config, language="en")
    case.clock[0] += timedelta(minutes=5, seconds=1)
    tick(tick_context(case, start_runs=False))
    assert case.tracker.issues[40].state == "open"


def test_child_without_a_row_is_remembered_so_the_next_tick_continues(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    _unlisted_child_after_a_failed_close(case, monkeypatch, FULL)
    latency = _slow_reads_before_parents(case, monkeypatch)
    seen = []
    for _ in range(6):
        latency.gate_tick(case)
        seen.append([call[0] for call in latency.calls if call[2] and call[0] != "issues_closed_since"])
        if case.tracker.issues[40].state == "closed":
            break
    assert case.tracker.issues[40].state == "closed", f"The sequence restarted: {seen}"
    assert seen[0][0] == "sub_issues" and "sub_issues" not in seen[-1], f"The last tick continues past it: {seen}"
    assert len(case.tracker.comments[40]) == 1 and "#5" in case.tracker.comments[40][0].body


def test_remembered_children_do_not_outlive_a_changed_summary(parent_case, monkeypatch):  # noqa: F811
    """A reopened child with no row changes the parent's summary, so the remembered read no longer counts."""
    case = parent_case
    _unlisted_child_after_a_failed_close(case, monkeypatch, FULL)
    latency = _slow_reads_before_parents(case, monkeypatch)
    latency.gate_tick(case)  # The read is remembered; the close runs out of budget.
    assert case.tracker.issues[40].state == "open"
    case.tracker.issues[5] = replace(case.tracker.issues[5], state="open", state_reason=None)
    _frozen_parent_listing(case, monkeypatch, 40, (("total", 1), ("completed", 0), ("percent_completed", 0)),
                           hidden={5})
    for _ in range(3):
        latency.gate_tick(case)
    assert case.tracker.issues[40].state == "open", "A child reopened since the read keeps the parent open"


def test_older_child_read_does_not_replace_newer_edges(parent_case):  # noqa: F811
    case = parent_case
    add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case, refresh=True)
    children = next(row for row in case.store.local_issues() if row["number"] == 10)["children"]
    case.store.record_child_read(10, [], started=case.clock[0] - timedelta(hours=1),
                                 baseline=case.store.read_baseline())
    row = next(row for row in case.store.local_issues() if row["number"] == 10)
    assert row["children"] == children and len(children) == 2


def _run_until(latency, case, done, ticks=6):
    seen = []
    for _ in range(ticks):
        latency.gate_tick(case)
        seen.append([call[0] for call in latency.calls if call[2] and call[0] != "issues_closed_since"])
        if done():
            break
    return seen


def test_card_moves_before_the_reference_read_can_spend_its_budget(parent_case, monkeypatch):  # noqa: F811
    """Native children ended and one unlisted reference stays open: the read repeats, but the card must move."""
    case = parent_case
    add_work(case, 5, closed=True)
    add_work(case, 8)
    add_record(case, 40, mapping={"a": 5}, edited=[8])
    case.tracker.add_sub_issue(40, 5)
    _frozen_parent_listing(case, monkeypatch, 40, FULL, hidden={8})
    latency = _slow_reads_before_parents(case, monkeypatch)
    seen = _run_until(latency, case, lambda: card_column(case.tracker, case.board_id, 40) == DONE)
    assert card_column(case.tracker, case.board_id, 40) == DONE, f"The move kept restarting: {seen}"
    assert case.tracker.issues[40].state == "open", "An open reference keeps the record open"
    latency.gate_tick(case)
    assert [call[0] for call in latency.calls if call[2] and call[0] != "issues_closed_since"] == [
        "reference_states"], "Only the fresh reference read repeats"


def test_lasting_total_mismatch_stops_rereading_and_the_only_parent_closes(parent_case, monkeypatch):  # noqa: F811
    """The summary keeps counting a child the endpoint never returns, and GitHub says all have ended."""
    case = parent_case
    add_work(case, 5, closed=True)
    case.tracker.add_issue(7, "A pull request", is_pull_request=True)
    add_record(case, 40, mapping={"a": 5}, edited=[7])
    case.tracker.add_sub_issue(40, 5)
    _frozen_parent_listing(case, monkeypatch, 40, (("total", 2), ("completed", 2), ("percent_completed", 100)))
    latency = _slow_reads_before_parents(case, monkeypatch)
    seen = _run_until(latency, case, lambda: case.tracker.issues[40].state == "closed")
    assert case.tracker.issues[40].state == "closed", f"The sequence restarted: {seen}"
    assert seen[0][0] == "sub_issues" and "sub_issues" not in seen[-1], seen
    assert len(case.tracker.comments[40]) == 1


def test_lasting_total_mismatch_with_open_work_never_closes(parent_case, monkeypatch):  # noqa: F811
    """A read that shows fewer children than the total must not hide the open work the summary counts."""
    case = parent_case
    add_work(case, 5, closed=True)
    add_record(case, 40, mapping={"a": 5})
    case.tracker.add_sub_issue(40, 5)
    _frozen_parent_listing(case, monkeypatch, 40, (("total", 2), ("completed", 1), ("percent_completed", 50)))
    latency = _slow_reads_before_parents(case, monkeypatch)
    seen = _run_until(latency, case, lambda: False)
    assert case.tracker.issues[40].state == "open", "The summary still counts open work"
    assert "sub_issues" not in seen[-1], f"A confirmed read need not repeat: {seen}"


def test_card_on_a_later_page_moves_within_the_tick_budget(parent_case, monkeypatch):  # noqa: F811
    """The issue sits on many projects, so finding its card takes two reads before the write."""
    case = parent_case
    children = add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    graphql = case.tracker.graphql

    def paged(query, variables):
        if "ForagentBoardIssue" not in query or variables["number"] != 10:
            return graphql(query, variables)
        if variables.get("cursor") is None:
            result = graphql(query, variables)
            result["data"]["repository"]["issue"]["projectItems"] = {
                "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "2"}}
            return result
        return graphql(query, {**variables, "cursor": None})

    monkeypatch.setattr(case.tracker, "graphql", paged)
    case.tracker.close_issue(children[0])
    latency = _slow_reads_before_parents(case, monkeypatch)
    seen = _run_until(latency, case, lambda: card_column(case.tracker, case.board_id, 10) == IN_PROGRESS)
    assert card_column(case.tracker, case.board_id, 10) == IN_PROGRESS, f"The move kept restarting: {seen}"
    assert len(seen) == 2 and len(seen[-1]) == 2, f"The second tick keeps the found card: {seen}"


def _card_on_page(graphql, number, page):
    """The issue's card sits on ``page``, after full pages of other projects."""
    def paged(query, variables):
        if "ForagentBoardIssue" not in query or variables["number"] != number:
            return graphql(query, variables)
        reached = int(variables.get("cursor") or 1)
        result = graphql(query, {**variables, "cursor": None})
        if reached < page:
            result["data"]["repository"]["issue"]["projectItems"] = {
                "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": str(reached + 1)}}
        return result
    return paged


@pytest.mark.parametrize("page", [3, 7])
def test_card_on_a_distant_page_moves_and_the_record_still_closes(parent_case, monkeypatch, page):  # noqa: F811
    """Each tick continues the search, then the move, then the reference read and the close."""
    case = parent_case
    add_work(case, 5, closed=True)
    case.tracker.add_issue(7, "A pull request", is_pull_request=True)
    add_record(case, 40, mapping={"a": 5}, edited=[7])
    case.tracker.add_sub_issue(40, 5)
    seed_card(case.tracker, case.board_id, 40, BACKLOG)
    monkeypatch.setattr(case.tracker, "graphql", _card_on_page(case.tracker.graphql, 40, page))
    latency = _slow_reads_before_parents(case, monkeypatch)
    seen = _run_until(latency, case, lambda: case.tracker.issues[40].state == "closed", ticks=page + 4)
    assert case.tracker.issues[40].state == "closed", f"The search restarted: {seen}"
    assert seen[-1] == ["reference_states", "close_issue"], f"The close comes last, on its own budget: {seen}"
    assert len(case.tracker.comments[40]) == 1


def test_missing_card_is_added_after_a_long_search(parent_case, monkeypatch):  # noqa: F811
    case = parent_case
    children = add_parent(case, ["unapproved", "unapproved"])
    gate_tick(case)
    gate_tick(case, refresh=True)
    items = case.tracker._projects.projects[case.board_id]["items"]
    for key in [key for key, item in items.items() if item["contentId"].endswith("#10")]:
        del items[key]
    case.store.record_board_value(10, case.board_id, "Status", None)
    monkeypatch.setattr(case.tracker, "graphql", _card_on_page(case.tracker.graphql, 10, 3))
    case.tracker.close_issue(children[0])
    latency = _slow_reads_before_parents(case, monkeypatch)

    def added():
        try:
            return card_column(case.tracker, case.board_id, 10) == IN_PROGRESS
        except StopIteration:  # No card yet, or added and still without a column.
            return False

    seen = _run_until(latency, case, added, ticks=8)
    assert added(), f"The search restarted: {seen}"
    assert sum(item["contentId"].endswith("#10") for item in items.values()) == 1, "One card, not one per retry"

