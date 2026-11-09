"""Guards for lock-corrections-beside-issue: controls that already hold on main (not in Done when)."""

from conftest import git_command
from lock_corrections_beside_case import (BRANCH, CLOSED, FIXED_TEST, HELPER, LOCKED, LOCKED_TEST,  # noqa: F401
                                          NEW_CLOSED, NEW_HELPER, NUMBER, OLD_CLOSED, OLD_HELPER, accepted,
                                          apply, case, correction, describe, offline_publication, on_origin,
                                          publish_and_check, without_issue_branch, world)


def test_world_control_own_lock_correction_still_publishes(world):
    """The world is sound: #2558's route (the issue's own Done when file) publishes here on main."""
    plan = correction({LOCKED: FIXED_TEST})
    row = publish_and_check(world, plan)
    assert accepted(row), describe(world, row)
    row = apply(world, plan)
    assert row["status"] == "published", describe(world, row)
    assert on_origin(world, "main", LOCKED) == FIXED_TEST
    assert ("branch", LOCKED, "fixed", "old") in {run[:4] for run in world.runs}, (
        f"The scripted measurement sees the corrected file on the issue branch; runs {world.runs}")


def _nothing_landed(world, name, old):
    assert on_origin(world, "main", name) == old, f"{name} must stay as it is on main"
    if git_command(world.origin, "for-each-ref", f"refs/heads/{BRANCH}"):
        assert git_command(world.origin, "rev-parse", f"refs/heads/{BRANCH}") == world.branch_head, (
            "The issue branch must not change")


def test_helper_correction_that_fails_on_the_branch_does_not_land(world):
    """Wrong value: the corrected helper does not make #7's locked test pass on the issue branch."""
    world.outcomes[("branch", LOCKED, "locked", "new")] = False
    plan = correction({HELPER: NEW_HELPER})
    row = publish_and_check(world, plan)
    assert not accepted(row), describe(world, row)
    if row is not None:
        apply(world, plan)
    _nothing_landed(world, HELPER, OLD_HELPER)


def test_helper_correction_whose_previous_version_passes_on_the_branch_does_not_land(world):
    """The previous helper already passes on the branch: nothing is proved stale."""
    world.outcomes[("branch", LOCKED, "locked", "old")] = True
    plan = correction({HELPER: NEW_HELPER})
    row = publish_and_check(world, plan)
    assert not accepted(row), describe(world, row)
    if row is not None:
        apply(world, plan)
    _nothing_landed(world, HELPER, OLD_HELPER)


def test_helper_correction_turning_a_main_green_test_red_is_not_published_to_main(world):
    """The corrected helper breaks closed #5's main-green lock: it cannot land on main ahead of #7."""
    world.outcomes[("main", CLOSED, "old", "new")] = False
    plan = correction({HELPER: NEW_HELPER})
    row = publish_and_check(world, plan)
    if row is not None:
        apply(world, plan)
    assert on_origin(world, "main", HELPER) == OLD_HELPER, (
        "A helper correction that turns a main-green test red must not be published to main on its own")


def test_closed_issue_lock_correction_without_an_issue_branch_is_refused(world):
    """No implementation to land with: the correction is refused as a separate publication."""
    without_issue_branch(world)
    plan = correction({CLOSED: NEW_CLOSED})
    row = publish_and_check(world, plan)
    assert row is None or row["status"] == "refused", (
        f"A separate publication of the closed-issue lock correction must be refused; got {describe(world, row)}")
    if row is not None:
        apply(world, plan)
    assert on_origin(world, "main", CLOSED) == OLD_CLOSED, (
        "A correction that would turn a main-green test red is never published to main on its own")


def test_closed_issue_lock_correction_that_fails_on_the_branch_does_not_land(world):
    """Wrong value: the corrected closed-issue lock does not pass on the issue branch."""
    world.outcomes[("branch", CLOSED, "new", "old")] = False
    plan = correction({CLOSED: NEW_CLOSED})
    row = publish_and_check(world, plan)
    assert not accepted(row), describe(world, row)
    if row is not None:
        apply(world, plan)
    _nothing_landed(world, CLOSED, OLD_CLOSED)
    assert on_origin(world, BRANCH, CLOSED) == OLD_CLOSED
