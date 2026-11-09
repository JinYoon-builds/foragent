"""Acceptance for order-accepts-existing-blocker (Jin r1301).

Public contract: an order's new issues and edits may name an existing issue ``#N`` in
``blocked_by``; publication records N as the GitHub blocker, like a plan-key dependency.
A ``#N`` that does not exist is refused before anything is written, naming ``#N``.
The fa-order skill wording (approve such work ahead) is not tested, per the PM answer.
Each test's docstring names the wrong implementation it rejects.
"""

import re

import pytest

from existing_blocker_world import (
    EDITED, MISSING, OPEN_BLOCKER, OTHER, blockers, created, edit, make_case, new_issue,
    plan, publish,
)
from test_order import publishing  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.fixture
def case(publishing, monkeypatch):  # noqa: F811
    return make_case(publishing, monkeypatch)


def order_records(case):
    return [issue for issue in case.tracker.list_issues() if "<!-- foragent:plan " in issue.body]


def assert_untouched_existing(case, *numbers, before):
    for number in numbers:
        issue = case.tracker.get_issue(number)
        assert (issue.title, issue.body, issue.state) == before[number], (
            f"Publication must not change existing issue #{number}"
        )


def snapshot(case):
    return {number: (issue.title, issue.body, issue.state)
            for number, issue in case.tracker.issues.items()}


def test_new_issue_records_an_existing_open_issue_as_its_blocker(case, capsys):
    """Reject refusing '#41', dropping the edge, recording the wrong number or putting the
    edge on the order record or on #41 itself; the issue stays approved (work approved ahead)."""
    before = snapshot(case)
    code, message = publish(case, capsys, plan("2026-11-09-wait-new",
                                               issues=[new_issue(blocked_by=[f"#{OPEN_BLOCKER}"])]),
                            "--approve")
    assert code == 0, f"An order may wait on existing open issue #{OPEN_BLOCKER}: {message}"
    child = created(case, "Build on the earlier work")
    assert blockers(case, child.number) == [OPEN_BLOCKER], (
        f"The new issue must be blocked by exactly #{OPEN_BLOCKER}"
    )
    assert "approved" in child.labels, "Waiting on an existing issue must not withhold approval"
    for record in order_records(case):
        assert blockers(case, record.number) == [], "The order record itself waits on nothing"
    for number in (OPEN_BLOCKER, EDITED, OTHER):
        assert blockers(case, number) == [], f"Existing issue #{number} must gain no blocker"
    assert_untouched_existing(case, OPEN_BLOCKER, EDITED, OTHER, before=before)
    assert case.tracker.get_issue(OPEN_BLOCKER).state == "open"


def test_existing_blocker_and_plan_key_are_both_recorded(case, capsys):
    """Reject letting '#N' replace or swallow plan-key dependencies of the same issue."""
    first = new_issue(key="base", title="Lay the base")
    waiting = new_issue(blocked_by=["base", f"#{OPEN_BLOCKER}"])
    code, message = publish(case, capsys, plan("2026-11-09-wait-mixed", issues=[first, waiting]),
                            "--approve")
    assert code == 0, message
    base = created(case, "Lay the base")
    child = created(case, "Build on the earlier work")
    assert blockers(case, child.number) == sorted([base.number, OPEN_BLOCKER])
    assert blockers(case, base.number) == []


def test_edit_records_an_existing_issue_as_the_edited_issues_blocker(case, capsys):
    """Reject refusing '#41' in edits, skipping the edge, or putting it on the wrong issue."""
    code, message = publish(case, capsys, plan("2026-11-09-wait-edit",
                                               edits=[edit(blocked_by=[f"#{OPEN_BLOCKER}"])]),
                            "--approve")
    assert code == 0, f"An edit may wait on existing issue #{OPEN_BLOCKER}: {message}"
    assert blockers(case, EDITED) == [OPEN_BLOCKER], (
        f"Edited #{EDITED} must be blocked by exactly #{OPEN_BLOCKER}"
    )
    for number in (OPEN_BLOCKER, OTHER):
        assert blockers(case, number) == []
    for record in order_records(case):
        assert blockers(case, record.number) == []
    titles = {issue.title for issue in case.tracker.list_issues()}
    assert "Build on the earlier work" not in titles, "An edit-only order creates no work issue"


def test_a_later_edit_replaces_the_existing_blocker(case, capsys):
    """Reject adding beside the old blocker: an edit's blocked_by is the full set, as for plan keys."""
    code, message = publish(case, capsys, plan("2026-11-09-wait-edit-1",
                                               edits=[edit(blocked_by=[f"#{OPEN_BLOCKER}"])]))
    assert code == 0, message
    assert blockers(case, EDITED) == [OPEN_BLOCKER]
    code, message = publish(case, capsys, plan("2026-11-09-wait-edit-2",
                                               edits=[edit(blocked_by=[f"#{OTHER}"])]))
    assert code == 0, message
    assert blockers(case, EDITED) == [OTHER], (
        f"A later edit naming #{OTHER} must leave #{EDITED} blocked by #{OTHER} only"
    )


@pytest.mark.parametrize("kind", ["issue", "edit"])
def test_missing_existing_issue_is_refused_before_anything_is_written(case, capsys, kind):
    """Reject writing issues or files before checking that #N exists, a refusal that does
    not name #N, and refusing a valid '#41' beside it (the same plan without #977 publishes)."""
    good = new_issue(blocked_by=[f"#{OPEN_BLOCKER}"])
    if kind == "issue":
        bad = {"issues": [good, new_issue(key="other", title="Other waiting work",
                                          blocked_by=[f"#{MISSING}"])], "edits": []}
    else:
        bad = {"issues": [good], "edits": [edit(blocked_by=[f"#{MISSING}"])]}
    before = snapshot(case)
    code, message = publish(case, capsys, plan("2026-11-09-wait-missing", **bad), "--approve")
    assert code != 0, f"#{MISSING} does not exist, so publication must be refused"
    assert re.search(rf"#{MISSING}(?!\d)", message), f"The refusal must name #{MISSING}: {message}"
    assert snapshot(case) == before, "A refused order must create or change no issue"
    assert not any(event[0] in ("issue", "commit") for event in case.events), (
        "A refused order must create no issue and commit no file"
    )
    assert all(case.tracker.blocked_by(number) == [] for number in before)

    fixed = {"issues": bad["issues"][:1], "edits": [edit()] if kind == "edit" else []}
    code, message = publish(case, capsys, plan("2026-11-09-wait-fixed", **fixed), "--approve")
    assert code == 0, f"Without #{MISSING} the order waiting on #{OPEN_BLOCKER} publishes: {message}"
    assert blockers(case, created(case, "Build on the earlier work").number) == [OPEN_BLOCKER]
