"""#837 with the real StateStore and RecordingTracker across listing refreshes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_order_record_refs import add_record, add_work, tick_again
from test_parent_follows_children import gate_tick, offline_only, parent_case  # noqa: F401
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


def test_record_with_more_unlisted_closed_references_than_one_tick_reads_closes(parent_case):  # noqa: F811
    from foragent import board_parents

    case = parent_case
    edited = list(range(100, 100 + board_parents.MAX_REFERENCE_READS + 1))
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
    assert case.tracker.get_issue(40).state == "open"
    tick_again(case)
    assert case.tracker.get_issue(40).state == "closed", "Reads carry over listing refreshes"
    assert sorted(reads) == edited, "Each unlisted reference is read exactly once"
