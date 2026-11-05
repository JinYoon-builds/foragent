"""Acceptance for one-close-path: an issue's PR and branch go with the issue, whichever way it closes.

Public contract:
- Closing an issue through an order's cleanup (``closes`` in ``fa order publish``) also closes
  the issue's open Foragent PR (head ``foragent/issue-N``) and deletes that branch, once.
- ``fa orch close`` keeps closing the issue, its PR and branch as today.
- Closing an issue with no PR still works on both paths.

Real CLI entry points; the tracker is the project's fake tracker, Git is the existing offline
fixtures. Each test's docstring names the wrong implementation it must reject.
"""

from types import SimpleNamespace

import pytest

from test_order import publishing  # noqa: F401
from test_order_board_cleanup import DECLINED, DUPLICATE, RESOLVED, cleanup_case, publish  # noqa: F401
from test_orch_close_superseded import BRANCH, close, superseded_case
from test_skills_autosync import isolated_home  # noqa: F401

OTHER_HEAD = "feature/empty-password"


def branch(n):
    return f"foragent/issue-{n}"


def open_prs(case, numbers):
    """Give each listed issue an open Foragent PR; return {issue: pr}."""
    return {n: case.tracker.create_pr(head=branch(n), base="main", title=f"Fix (#{n})", body=f"Closes #{n}")
            for n in numbers}


def pr_state(case, pr):
    return case.tracker.get_issue(pr).state


def record_closes(tracker, monkeypatch):
    """Count writes at the GitHub service boundary, including duplicate close requests."""
    calls = SimpleNamespace(issues=[], prs=[])
    existing = set(tracker.issues)
    close_issue, close_pr = tracker.close_issue, tracker.close_pr

    def issue(number, *args, **kwargs):
        # Publishing also creates/closes its own order-record issue. Observe the
        # issues/PRs present before the action, which are this contract's targets.
        if number in existing:
            calls.issues.append(number)
        return close_issue(number, *args, **kwargs)

    def pr(number, *args, **kwargs):
        calls.prs.append(number)
        return close_pr(number, *args, **kwargs)

    monkeypatch.setattr(tracker, "close_issue", issue)
    monkeypatch.setattr(tracker, "close_pr", pr)
    return calls


def test_cleanup_close_closes_the_issue_pr_and_branch_exactly_once(cleanup_case, capsys, monkeypatch):
    """Reject a cleanup that closes only the issue (PR #1875 stayed open), one that closes the PR
    but keeps its branch, one that touches a declined item's PR or a PR that is not Foragent's,
    and a rerun that closes or deletes again."""
    case = cleanup_case
    prs = open_prs(case, (RESOLVED, DUPLICATE, DECLINED))
    foreign = case.tracker.create_pr(head=OTHER_HEAD, base="main", title="Someone's fix",
                                     body=f"Fixes #{RESOLVED}")
    calls = record_closes(case.tracker, monkeypatch)

    code, message = publish(case, capsys, "--approve")
    assert code == 0, message
    assert case.tracker.get_issue(RESOLVED).state == "closed"
    assert case.tracker.get_issue(DUPLICATE).state == "closed"
    assert pr_state(case, prs[RESOLVED]) == "closed", "The resolved issue's open Foragent PR must close with it"
    assert pr_state(case, prs[DUPLICATE]) == "closed", "A folded duplicate's open Foragent PR must close with it"
    assert sorted(calls.prs) == sorted([prs[RESOLVED], prs[DUPLICATE]]), "Each PR closes once"
    assert sorted(calls.issues) == [RESOLVED, DUPLICATE], "Each issue closes once"
    assert sorted(case.tracker.deleted_branches) == sorted([branch(RESOLVED), branch(DUPLICATE)]), (
        f"Each closed issue's Foragent branch is deleted exactly once: {case.tracker.deleted_branches}"
    )
    assert pr_state(case, prs[DECLINED]) == "open", "A declined item's PR must stay open"
    assert pr_state(case, foreign) == "open", "A PR that is not Foragent's must stay open"

    code, message = publish(case, capsys, "--approve")
    assert code == 0, message
    assert sorted(case.tracker.deleted_branches) == sorted([branch(RESOLVED), branch(DUPLICATE)]), (
        "Publishing the same plan again must delete nothing more"
    )
    assert sorted(calls.prs) == sorted([prs[RESOLVED], prs[DUPLICATE]])
    assert sorted(calls.issues) == [RESOLVED, DUPLICATE]


def test_cleanup_close_without_a_pr_still_closes(cleanup_case, capsys, monkeypatch):
    """Reject a cleanup that fails, or skips the issue, when the issue has no PR, and one that
    closes a PR of another issue in its place."""
    case = cleanup_case
    unrelated = open_prs(case, (DECLINED,))[DECLINED]
    calls = record_closes(case.tracker, monkeypatch)

    code, message = publish(case, capsys, "--approve")
    assert code == 0, message
    assert case.tracker.get_issue(DUPLICATE).state == "closed", "An issue with no PR must still close"
    assert len(case.tracker.list_comments(DUPLICATE)) == 1
    assert case.tracker.get_issue(RESOLVED).state == "closed"
    assert sorted(calls.issues) == [RESOLVED, DUPLICATE]
    assert calls.prs == []
    assert pr_state(case, unrelated) == "open"
    assert case.tracker.deleted_branches == []


@pytest.mark.parametrize("options", [(), ("--approve", "--dry-run")], ids=["unapproved", "dry-run"])
def test_unapproved_dry_run_or_refused_cleanup_leaves_prs_open(cleanup_case, capsys, monkeypatch, options):
    """Reject closing a PR for a close that was not approved, for a dry run, or for a refused
    publication."""
    case = cleanup_case
    prs = open_prs(case, (RESOLVED, DUPLICATE))
    calls = record_closes(case.tracker, monkeypatch)

    code, message = publish(case, capsys, *options)
    assert code == 0, message
    assert all(pr_state(case, pr) == "open" for pr in prs.values())
    assert all(case.tracker.get_issue(n).state == "open" for n in prs)
    assert calls.prs == [] and calls.issues == []
    assert case.tracker.deleted_branches == []


def test_another_items_approval_leaves_the_unapproved_pr_open(cleanup_case, capsys, monkeypatch):
    case = cleanup_case
    pr = open_prs(case, (DUPLICATE,))[DUPLICATE]
    calls = record_closes(case.tracker, monkeypatch)
    code, message = publish(case, capsys, "--approve-numbers", str(RESOLVED))
    assert code == 0, message
    assert case.tracker.get_issue(RESOLVED).state == "closed"
    assert case.tracker.get_issue(DUPLICATE).state == "open"
    assert pr_state(case, pr) == "open", "An unapproved close must leave its PR open"
    assert calls.issues == [RESOLVED] and calls.prs == []
    assert case.tracker.deleted_branches == []


def test_running_target_refusal_leaves_prs_open(cleanup_case, capsys, monkeypatch):
    """Reject closing PRs before the publication refuses a running target."""
    case = cleanup_case
    prs = open_prs(case, (RESOLVED, DUPLICATE))
    case.tracker.add_labels(DUPLICATE, ["running"])
    calls = record_closes(case.tracker, monkeypatch)

    code, message = publish(case, capsys, "--approve")
    assert code != 0, message
    assert all(pr_state(case, pr) == "open" for pr in prs.values())
    assert case.tracker.deleted_branches == []
    assert calls.prs == [] and calls.issues == []


def test_orch_close_still_closes_issue_pr_and_branch(git_history, tmp_path, monkeypatch):
    """Reject a refactor that drops the PR close or branch delete from `fa orch close`, or doubles it."""
    case = superseded_case(git_history, tmp_path, monkeypatch)
    calls = record_closes(case.tracker, monkeypatch)
    assert close(case) == 0
    assert case.tracker.get_issue(1).state == "closed"
    assert case.tracker.get_issue(case.pr).state == "closed"
    assert case.tracker.deleted_branches == [BRANCH], case.tracker.deleted_branches
    assert calls.prs == [case.pr] and calls.issues == [1]
    assert close(case) == 0, "Closing again is a successful no-op"
    assert case.tracker.deleted_branches == [BRANCH], "A repeated close must delete nothing more"
    assert calls.prs == [case.pr] and calls.issues == [1]


def test_orch_close_without_a_pr_still_closes(git_history, tmp_path, monkeypatch):
    """Reject an `fa orch close` that fails, or deletes a branch, when the issue has no open PR."""
    case = superseded_case(git_history, tmp_path, monkeypatch)
    case.tracker.close_pr(case.pr, delete_branch=False)
    calls = record_closes(case.tracker, monkeypatch)
    assert close(case) == 0
    assert case.tracker.get_issue(1).state == "closed"
    assert case.tracker.deleted_branches == []
    assert calls.prs == [] and calls.issues == [1]
