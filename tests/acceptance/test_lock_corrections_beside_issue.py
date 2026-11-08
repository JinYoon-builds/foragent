"""Acceptance tests for lock-corrections-beside-issue (Jin 11-08, r1210).

An order edit of open issue #7 (published on the statement route) may carry corrected versions of
locked acceptance files outside #7's own Done when: the locked test helper #7's tests import, or
closed issue #5's locked test that #7's change contradicts. The correction is proved on the issue
branch (corrected files pass, previous ones fail) and must not turn a main-green test red:

1. a corrected helper that keeps main green publishes with --approve without test preparation and
   lands on main ahead of the issue, without the PM;
2. a corrected closed-issue lock that would turn a main-green test red is not published to main on
   its own; it is accepted only to land in the same PR as #7's implementation, i.e. on the issue
   branch ``foragent/issue-7`` that the PR merges.

The world (real local Git with a bare origin, scripted measurements) is in
``lock_corrections_beside_case.py``. Controls that already hold on main are in
``test_lock_corrections_beside_issue_guard.py``.
"""

from lock_corrections_beside_case import (BRANCH, CLOSED, HELPER, LOCKED, LOCKED_TEST, NEW_CLOSED,  # noqa: F401
                                          NEW_HELPER, NUMBER, OLD_CLOSED, accepted, apply, case,
                                          commits_touching, correction, describe, offline_publication,
                                          on_origin, publish_and_check, world)
from foragent import orch_inbox, order_prepare


def _no_preparation(monkeypatch):
    """Record any test preparation (writer, reviewer) started for the correction."""
    started = []

    def refuse(*args, **kwargs):
        started.append("prepare")
        raise AssertionError("test preparation started")

    monkeypatch.setattr(order_prepare, "prepare", refuse)
    return started


def _measured(world):
    return {(spot, name, helper, test) for spot, name, test, helper, _command in world.runs}


def _stands_approved(world):
    labels = set(world.tracker.get_issue(NUMBER).labels)
    assert "approved" in labels, f"Issue #{NUMBER} must stand approved; labels {labels}"
    assert "needs-human" not in labels, f"The correction must not wait on a person; labels {labels}"
    assert orch_inbox.questions(world.case.store) == [], "No question for the PM or a person"


# Statement 1: a corrected locked helper, proved on the branch and keeping main green -----------


def test_corrected_helper_publishes_with_approve_without_preparation(world, monkeypatch):
    started = _no_preparation(monkeypatch)
    plan = correction({HELPER: NEW_HELPER})
    row = publish_and_check(world, plan)
    assert accepted(row), (
        "An edit carrying only a corrected locked helper of open issue #7, proved on the issue branch and "
        f"keeping main green, must pass the publication check; got {describe(world, row)}")
    row = apply(world, plan)
    assert row["status"] == "published", describe(world, row)
    assert on_origin(world, "main", HELPER) == NEW_HELPER, "The corrected helper must land on main"
    assert on_origin(world, "main", LOCKED) == LOCKED_TEST and on_origin(world, "main", CLOSED) == OLD_CLOSED, (
        "Only the helper changes on main")
    assert started == [], "No test preparation runs for a proved helper correction"
    _stands_approved(world)


def test_helper_correction_is_proved_with_both_versions_on_the_issue_branch(world):
    row = publish_and_check(world, correction({HELPER: NEW_HELPER}))
    assert accepted(row), describe(world, row)
    seen = _measured(world)
    assert ("branch", LOCKED, "new", "locked") in seen, (
        f"#7's locked test must be measured with the corrected helper on the issue branch; runs {world.runs}")
    assert ("branch", LOCKED, "old", "locked") in seen, (
        f"#7's locked test must be measured with the previous helper on the issue branch; runs {world.runs}")


def test_helper_correction_runs_main_green_importers_on_main(world):
    row = publish_and_check(world, correction({HELPER: NEW_HELPER}))
    assert accepted(row), describe(world, row)
    assert ("main", CLOSED, "new", "old") in _measured(world), (
        "A main-green test that imports the corrected helper (closed #5's lock) must run on main with the "
        f"corrected helper, so the correction cannot turn it red; runs {world.runs}")


def test_helper_correction_lands_once(world):
    """Doubled effect: a second apply pass lands nothing more on main."""
    plan = correction({HELPER: NEW_HELPER})
    row = publish_and_check(world, plan)
    assert accepted(row), describe(world, row)
    apply(world, plan)
    apply(world, plan)
    assert on_origin(world, "main", HELPER) == NEW_HELPER
    assert commits_touching(world, "main", HELPER, since=world.main) == 1, (
        "The corrected helper lands on main in exactly one commit")


# Statement 2: a corrected closed-issue lock that would turn main red lands with the issue ------


def test_closed_issue_lock_correction_lands_in_the_issue_pr_not_on_main(world, monkeypatch):
    started = _no_preparation(monkeypatch)
    plan = correction({CLOSED: NEW_CLOSED})
    row = publish_and_check(world, plan)
    assert row is not None and row["status"] != "refused", (
        "A corrected closed-issue lock, proved on the issue branch, must be accepted to land with #7's "
        f"implementation; got {describe(world, row)}")
    apply(world, plan)
    assert on_origin(world, "main", CLOSED) == OLD_CLOSED, (
        "A correction that would turn a main-green test red must not be published to main on its own")
    assert on_origin(world, BRANCH, CLOSED) == NEW_CLOSED, (
        f"The corrected closed-issue lock must land in the same PR as #7's implementation (branch {BRANCH})")
    assert on_origin(world, BRANCH, "src/widget.py") is not None, "#7's implementation stays on its branch"
    assert started == [], "No test preparation runs for a proved correction"
    _stands_approved(world)


def test_closed_issue_lock_correction_is_proved_on_the_branch_and_measured_red_on_main(world):
    row = publish_and_check(world, correction({CLOSED: NEW_CLOSED}))
    assert row is not None and row["status"] != "refused", describe(world, row)
    seen = _measured(world)
    assert ("branch", CLOSED, "old", "new") in seen, (
        f"The corrected closed-issue lock must be measured on the issue branch; runs {world.runs}")
    assert ("branch", CLOSED, "old", "old") in seen, (
        f"The previous closed-issue lock must be measured on the issue branch; runs {world.runs}")
    assert ("main", CLOSED, "old", "new") in seen, (
        f"The corrected closed-issue lock must be measured on main to find it would turn red; runs {world.runs}")


def test_closed_issue_lock_correction_lands_on_the_branch_once(world):
    """Doubled effect: a second apply pass adds no second correction commit to the issue branch."""
    plan = correction({CLOSED: NEW_CLOSED})
    row = publish_and_check(world, plan)
    assert row is not None and row["status"] != "refused", describe(world, row)
    apply(world, plan)
    apply(world, plan)
    assert on_origin(world, BRANCH, CLOSED) == NEW_CLOSED
    assert commits_touching(world, BRANCH, CLOSED, since=world.branch_head) == 1, (
        "The corrected closed-issue lock lands on the issue branch in exactly one commit")
