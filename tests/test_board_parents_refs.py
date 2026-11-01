"""#837 with the real StateStore and RecordingTracker across listing refreshes."""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_order_record_refs import add_record, add_work, tick_again
from test_parent_follows_children import (
    BACKLOG, IN_PROGRESS, add_parent, card_column, gate_tick, offline_only, parent_case,  # noqa: F401
)
from test_skills_autosync import isolated_home  # noqa: F401

from foragent.tracker import TrackerError


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
