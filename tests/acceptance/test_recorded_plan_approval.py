"""Acceptance for approval-record (Jin 11-08, r1265). Locked by Foragent; do not edit.

Planning records a person's first-hand approval of a plan's scope; the orchestrator
publishes its own draft with ``--approve`` and that record when the scope matches.
Each test's docstring names the wrong implementation it must reject.
"""

from copy import deepcopy

import pytest

from recorded_approval_world import (
    ALPHA, BETA, GAMMA, assert_recorded_evidence, child, item, make_case, publish, record,
)
from test_order import publishing  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.fixture
def case(publishing, monkeypatch):
    return make_case(publishing, monkeypatch)


def titles(plan):
    return {entry["key"]: entry["title"] for entry in plan["issues"]}


def test_recorded_approval_approves_every_recorded_issue_with_its_evidence(case, capsys):
    """Reject a record that is ignored, approval of only some issues, evidence that
    shows other words, the publish time instead of the recorded time, or evidence twice."""
    window = record(case, capsys)
    code, output = publish(case, capsys, "--approve", "--recorded-approval")
    assert code == 0, f"A draft whose scope matches the record must publish: {output}"
    for key, title in titles(case.plan).items():
        issue = child(case, title)
        assert issue.state == "open"
        assert "approved" in issue.labels, f"{key} is covered by the record and must be approved"
        assert_recorded_evidence(case, issue, window)


@pytest.mark.parametrize("change", ["goal-differs", "key-added"])
def test_scope_outside_the_record_is_refused_and_named(case, capsys, change):
    """Reject granting approval beyond the record: a changed goal or an extra key must
    refuse the whole publication, name that issue key, and publish nothing at all."""
    record(case, capsys)
    if change == "goal-differs":
        case.plan["issues"][1]["goal"] = "Let owners download every report ever sent as CSV."
        named = BETA
    else:
        case.plan["issues"].append(item(GAMMA, "Email the CSV", "Email the CSV export to finance."))
        named = GAMMA
    issues_before = deepcopy(case.tracker.issues)
    code, output = publish(case, capsys, "--approve", "--recorded-approval")
    assert code != 0, f"Scope beyond the recorded approval must be refused: {output}"
    assert named in output, f"The refusal must name {named}: {output}"
    assert case.tracker.issues == issues_before, "A refused publication creates or changes no issue"
    assert case.events == [], "A refused publication writes no Git files"


def test_publishing_a_subset_of_the_recorded_issues_is_approved(case, capsys):
    """Reject requiring every recorded issue to be published before the record counts."""
    window = record(case, capsys)
    beta_title = titles(case.plan)[BETA]
    case.plan["issues"] = [entry for entry in case.plan["issues"] if entry["key"] == ALPHA]
    code, output = publish(case, capsys, "--approve", "--recorded-approval")
    assert code == 0, f"Publishing fewer of the recorded issues is covered: {output}"
    issue = child(case, titles(case.plan)[ALPHA])
    assert "approved" in issue.labels, "The recorded issue published alone must be approved"
    assert_recorded_evidence(case, issue, window)
    assert all(entry.title != beta_title for entry in case.tracker.list_issues()), (
        "The recorded issue left out of this plan must not be published")
