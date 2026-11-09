"""Regression controls for checked-pr-lands-first, statement 3 (outside Done when).

When no pull request or batch is ready to land, queued order publications and
main write requests apply as today: in the owner pass that sees them. These pass
on current main and must keep passing after the change. The Done when check of
statement 3 is in test_checked_pr_lands_first.py (it needs the new order first).

World: checked_first_world.py on the locked `proto` merge-path world.
"""

from checked_first_world import (
    MEMORY, MEMORY_TEXT, NEIGHBOR, PUBLICATION, check_starts, checked_single_pr, commits_touching, install,
    is_ancestor, landed_merge, main_text, main_tip, memory_status, queue_memory, queue_publication, support,
)
from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401


def merge_within(case, pr, passes=4):
    for _ in range(passes):
        if landed_merge(case, pr):
            break
        support.gate(case)
    return landed_merge(case, pr)


def test_with_no_queued_pr_the_writes_apply_in_the_first_pass(proto, monkeypatch):
    case = proto
    install(case, monkeypatch)
    before = main_tip(case)
    row = queue_memory(case)
    queue_publication(case)
    support.gate(case)
    assert memory_status(case, row) == "done" and main_text(case, MEMORY) == MEMORY_TEXT
    assert case.publication["applied_on"] and is_ancestor(case, before, case.publication["applied_on"][0])
    assert len(commits_touching(case, MEMORY, before)) == 1 and len(commits_touching(case, PUBLICATION, before)) == 1


def test_a_pr_whose_ci_is_pending_does_not_hold_the_writes(proto, monkeypatch):
    case = proto
    install(case, monkeypatch)
    support.queue_workers(case, [5])
    pr = support.pr_for(case, 5)
    case.tracker.set_pr_checks(pr.number, "pending")
    before = main_tip(case)
    row = queue_memory(case)
    queue_publication(case)
    support.gate(case)
    assert memory_status(case, row) == "done", "The memory write applies while #5's CI is pending"
    assert len(case.publication["applied_on"]) == 1, "The publication applies while #5's CI is pending"
    assert landed_merge(case, pr) is None
    assert len(commits_touching(case, MEMORY, before)) == 1
    case.tracker.set_pr_checks(pr.number, "pass")
    merged = merge_within(case, pr)
    assert merged is not None and is_ancestor(case, case.publication["applied_on"][0], merged["main"])
    assert main_text(case, MEMORY) == MEMORY_TEXT and main_text(case, "src/member_5.py") == "VALUE = 5"


def test_a_pr_checked_on_an_older_main_does_not_hold_the_writes(proto, monkeypatch, capsys):
    """Main moved by someone else after #5's checks: #5 is not checked on the current main."""
    case = proto
    install(case, monkeypatch)
    pr, head, checked = checked_single_pr(case, capsys)
    case.history.tip = main_tip(case)
    case.history.update({NEIGHBOR: "OTHER = 'moved again by someone else'\n"}, message="Another main move")
    moved = main_tip(case)
    row = queue_memory(case)
    support.gate(case)
    assert memory_status(case, row) == "done", "The memory write applies; #5 is not ready on the current main"
    assert len(commits_touching(case, MEMORY, moved)) == 1
    merged = merge_within(case, pr)
    assert merged is not None, "#5 still merges after its re-check on the moved main"
    assert merged["head"] != head and is_ancestor(case, moved, merged["main"])
    assert check_starts(capsys.readouterr().out, 5), "#5 is checked again on the main that moved under it"
    assert main_text(case, MEMORY) == MEMORY_TEXT and main_text(case, "src/member_5.py") == "VALUE = 5"
