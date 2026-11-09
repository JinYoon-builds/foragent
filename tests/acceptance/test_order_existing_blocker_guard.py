"""Regression guard for order-accepts-existing-blocker (passes on main; not in Done when).

Plan-key blocked_by keeps working: edges are recorded, unknown keys are refused.
"""

import pytest

from existing_blocker_world import (
    EDITED, OPEN_BLOCKER, blockers, created, edit, make_case, new_issue, plan, publish,
)
from test_order import publishing  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.fixture
def case(publishing, monkeypatch):  # noqa: F811
    return make_case(publishing, monkeypatch)


def test_plan_key_dependency_is_recorded(case, capsys):
    first = new_issue(key="base", title="Lay the base")
    waiting = new_issue(blocked_by=["base"])
    code, message = publish(case, capsys, plan("2026-11-09-guard-key", issues=[first, waiting]),
                            "--approve")
    assert code == 0, message
    base = created(case, "Lay the base")
    assert blockers(case, created(case, "Build on the earlier work").number) == [base.number]
    assert blockers(case, base.number) == []


def test_edit_plan_key_dependency_is_recorded_and_empty_clears(case, capsys):
    first = new_issue(key="base", title="Lay the base")
    code, message = publish(case, capsys, plan("2026-11-09-guard-edit", issues=[first],
                                               edits=[edit(blocked_by=["base"])]))
    assert code == 0, message
    assert blockers(case, EDITED) == [created(case, "Lay the base").number]
    code, message = publish(case, capsys, plan("2026-11-09-guard-clear", edits=[edit(blocked_by=[])]))
    assert code == 0, message
    assert blockers(case, EDITED) == []


@pytest.mark.parametrize("kind", ["issue", "edit"])
def test_unknown_plan_key_is_still_refused_without_writes(case, capsys, kind):
    value = (plan("2026-11-09-guard-unknown", issues=[new_issue(blocked_by=["nope"])]) if kind == "issue"
             else plan("2026-11-09-guard-unknown", edits=[edit(blocked_by=["nope"])]))
    before = set(case.tracker.issues)
    code, message = publish(case, capsys, value, "--approve")
    assert code != 0
    assert "nope" in message, message
    assert set(case.tracker.issues) == before
    assert not any(event[0] in ("issue", "commit") for event in case.events)
    assert blockers(case, OPEN_BLOCKER) == [] and blockers(case, EDITED) == []
