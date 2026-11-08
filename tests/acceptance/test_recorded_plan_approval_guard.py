"""Regression guard for approval-record: publishing without a record behaves as today.

Not part of Done when (these pass on main). Locked by Foragent; do not edit.
"""

import pytest

from recorded_approval_world import WORDS, child, evidence_text, make_case, publish
from test_order import publishing  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.fixture
def case(publishing, monkeypatch):
    return make_case(publishing, monkeypatch)


def test_choice_free_fix_still_publishes_with_approve_after_a_clean_preread(case, capsys):
    """Reject requiring a record for every --approve publication."""
    code, output = publish(case, capsys, "--approve")
    assert code == 0, output
    for entry in case.plan["issues"]:
        issue = child(case, entry["title"])
        assert "approved" in issue.labels
        assert WORDS not in evidence_text(case, issue)


def test_publishing_without_approve_leaves_issues_unapproved(case, capsys):
    """Reject treating publication itself as approval."""
    code, output = publish(case, capsys)
    assert code == 0, output
    for entry in case.plan["issues"]:
        assert "approved" not in child(case, entry["title"]).labels
