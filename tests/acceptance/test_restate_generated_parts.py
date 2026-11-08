"""Acceptance tests for restate-generated-parts (bug 11-09: #2734, #2352, #2840 blocked).

Open issue #7 went through test preparation: its published body ends with the generated
``## Preparation notes`` section and the plan-child marker that ``fa order publish`` writes. The edit
is taken from ``fa order edit 7 --json`` and only carries corrected locked files. Such a restating edit
must be a lock-only correction, as for an issue without the generated parts:

1. a corrected locked helper proved on the issue branch and keeping main green publishes with
   --approve without test preparation (and lands on main);
2. a corrected closed-issue lock that would turn a main-green test red is accepted to land in the
   issue's own PR (branch ``foragent/issue-7``), not refused for missing reviewed tests;
3. an edit that changes an authored section (goal, a statement, files, Done when) still needs reviewed
   tests, while the exact restate of the same issue does not.

World: ``restate_generated_case.py`` over ``lock_corrections_beside_case.py``. Controls that already
hold on main are in ``test_restate_generated_parts_guard.py``.
"""

import pytest

from lock_corrections_beside_case import (BRANCH, CLOSED, FIXED_TEST, HELPER, LOCKED, LOCKED_TEST,  # noqa: F401
                                          NEW_CLOSED, NEW_HELPER, NUMBER, OLD_CLOSED, OLD_HELPER, accepted,
                                          apply, case, commits_touching, describe, offline_publication,
                                          on_origin, publish_and_check, world)
from restate_generated_case import correction_from_seed, nothing_prepared, prepared, seed  # noqa: F401
from foragent import orch_inbox

REVIEWED_REFUSAL = "no reviewed tests"


def _stands_approved(world):
    labels = set(world.tracker.get_issue(NUMBER).labels)
    assert "approved" in labels, f"Issue #{NUMBER} must stand approved; labels {labels}"
    assert "needs-human" not in labels, f"The correction must not wait on a person; labels {labels}"
    assert orch_inbox.questions(world.case.store) == [], "No question for the PM or a person"


def _not_refused_for_reviewed_tests(world, row):
    assert world.refusal is None or REVIEWED_REFUSAL not in world.refusal, (
        "A lock-only correction of a prepared issue must not be refused for missing reviewed tests; "
        f"got {describe(world, row)}")


def test_seed_of_a_prepared_issue_restates_it(prepared, monkeypatch, capsys):
    """The world is what the bug needs: the seed carries none of the generated parts."""
    seeded = seed(prepared, monkeypatch, capsys)
    text = repr(seeded)
    assert "Preparation notes" not in text and "plan-child" not in text, seeded
    assert "## Preparation notes" in prepared.tracker.get_issue(NUMBER).body
    # And the restate of it is a lock-only correction (fails on main: refused for reviewed tests).
    started = nothing_prepared(monkeypatch)
    row = publish_and_check(prepared, correction_from_seed(seeded, {HELPER: NEW_HELPER}))
    _not_refused_for_reviewed_tests(prepared, row)
    assert started == []


# Statement 1: a corrected locked helper keeping main green publishes with --approve -----------


def test_prepared_issue_helper_correction_publishes_with_approve_without_preparation(prepared, monkeypatch,
                                                                                    capsys):
    seeded = seed(prepared, monkeypatch, capsys)
    started = nothing_prepared(monkeypatch)
    plan = correction_from_seed(seeded, {HELPER: NEW_HELPER})
    row = publish_and_check(prepared, plan)
    _not_refused_for_reviewed_tests(prepared, row)
    assert accepted(row), (
        "An edit from fa order edit of a prepared issue, carrying only a corrected locked helper proved on "
        f"the issue branch and keeping main green, must pass the publication check; got {describe(prepared, row)}")
    row = apply(prepared, plan)
    assert row["status"] == "published", describe(prepared, row)
    assert on_origin(prepared, "main", HELPER) == NEW_HELPER, "The corrected helper must land on main"
    assert on_origin(prepared, "main", LOCKED) == LOCKED_TEST and on_origin(prepared, "main", CLOSED) == OLD_CLOSED, (
        "Only the helper changes on main")
    assert started == [], "No test preparation runs for a proved helper correction"
    _stands_approved(prepared)


def test_prepared_issue_helper_correction_is_proved_on_the_branch_and_main(prepared, monkeypatch, capsys):
    seeded = seed(prepared, monkeypatch, capsys)
    row = publish_and_check(prepared, correction_from_seed(seeded, {HELPER: NEW_HELPER}))
    assert accepted(row), describe(prepared, row)
    seen = {run[:4] for run in prepared.runs}
    assert ("branch", LOCKED, "locked", "new") in seen and ("branch", LOCKED, "locked", "old") in seen, (
        f"#7's locked test must be measured on the issue branch with both helpers; runs {prepared.runs}")
    assert ("main", CLOSED, "old", "new") in seen, (
        f"Main-green importers must run on main with the corrected helper; runs {prepared.runs}")


def test_prepared_issue_helper_correction_lands_once(prepared, monkeypatch, capsys):
    """Doubled effect: a second apply pass lands nothing more on main."""
    seeded = seed(prepared, monkeypatch, capsys)
    plan = correction_from_seed(seeded, {HELPER: NEW_HELPER})
    row = publish_and_check(prepared, plan)
    assert accepted(row), describe(prepared, row)
    apply(prepared, plan)
    apply(prepared, plan)
    assert on_origin(prepared, "main", HELPER) == NEW_HELPER
    assert commits_touching(prepared, "main", HELPER, since=prepared.main) == 1, (
        "The corrected helper lands on main in exactly one commit")


def test_prepared_issue_own_lock_correction_publishes_without_preparation(prepared, monkeypatch, capsys):
    """#2558's own-lock route on a prepared issue: the corrected own Done when file publishes."""
    seeded = seed(prepared, monkeypatch, capsys)
    started = nothing_prepared(monkeypatch)
    plan = correction_from_seed(seeded, {LOCKED: FIXED_TEST})
    row = publish_and_check(prepared, plan)
    _not_refused_for_reviewed_tests(prepared, row)
    assert accepted(row), describe(prepared, row)
    row = apply(prepared, plan)
    assert row["status"] == "published", describe(prepared, row)
    assert on_origin(prepared, "main", LOCKED) == FIXED_TEST
    assert started == []


# Statement 2: a corrected closed-issue lock lands in the issue's own PR -----------------------


def test_prepared_issue_closed_lock_correction_lands_in_the_issue_pr(prepared, monkeypatch, capsys):
    seeded = seed(prepared, monkeypatch, capsys)
    started = nothing_prepared(monkeypatch)
    plan = correction_from_seed(seeded, {CLOSED: NEW_CLOSED})
    row = publish_and_check(prepared, plan)
    _not_refused_for_reviewed_tests(prepared, row)
    assert row is not None and row["status"] != "refused", (
        "A corrected closed-issue lock of a prepared issue, proved on the issue branch, must be accepted to "
        f"land with #7's implementation; got {describe(prepared, row)}")
    apply(prepared, plan)
    assert on_origin(prepared, "main", CLOSED) == OLD_CLOSED, (
        "A correction that would turn a main-green test red must not be published to main on its own")
    assert on_origin(prepared, BRANCH, CLOSED) == NEW_CLOSED, (
        f"The corrected closed-issue lock must land in #7's PR (branch {BRANCH})")
    assert on_origin(prepared, BRANCH, "src/widget.py") is not None, "#7's implementation stays on its branch"
    assert started == [], "No test preparation runs for a proved correction"
    _stands_approved(prepared)


def test_prepared_issue_closed_lock_correction_lands_on_the_branch_once(prepared, monkeypatch, capsys):
    """Doubled effect: a second apply pass adds no second correction commit to the issue branch."""
    seeded = seed(prepared, monkeypatch, capsys)
    plan = correction_from_seed(seeded, {CLOSED: NEW_CLOSED})
    row = publish_and_check(prepared, plan)
    assert row is not None and row["status"] != "refused", describe(prepared, row)
    apply(prepared, plan)
    apply(prepared, plan)
    assert on_origin(prepared, BRANCH, CLOSED) == NEW_CLOSED
    assert commits_touching(prepared, BRANCH, CLOSED, since=prepared.branch_head) == 1, (
        "The corrected closed-issue lock lands on the issue branch in exactly one commit")


# Statement 3: an authored change still needs reviewed tests (contrasted with the restate) -----


def _authored_change(seeded, field):
    edit = dict(seeded)
    if field == "goal":
        edit["goal"] = seeded["goal"] + " Gears count twice."
    elif field == "statement":
        edit["statements"] = [*seeded["statements"], "Counting gears counts each gear twice."]
    elif field == "files":
        edit["files"] = [*seeded["files"], "src/gear.py"]
    else:
        edit["done_when"] = [*seeded["done_when"], f"uv run pytest -q {CLOSED}"]
    return edit


@pytest.mark.parametrize("field", ["goal", "statement", "files", "done_when"])
def test_prepared_issue_authored_change_needs_reviewed_tests_but_restate_does_not(prepared, monkeypatch, capsys,
                                                                                 field):
    seeded = seed(prepared, monkeypatch, capsys)
    changed = correction_from_seed(_authored_change(seeded, field), {HELPER: NEW_HELPER},
                                   plan_id=f"2026-11-09-widget-{field}")
    row = publish_and_check(prepared, changed)
    assert row is None and prepared.refusal is not None and REVIEWED_REFUSAL in prepared.refusal, (
        f"An edit that changes the {field} of a prepared issue must still need reviewed tests; "
        f"got {describe(prepared, row)}")
    assert on_origin(prepared, "main", HELPER) == OLD_HELPER, "A refused edit lands nothing"
    restated = correction_from_seed(seeded, {HELPER: NEW_HELPER})
    row = publish_and_check(prepared, restated)
    assert accepted(row), (
        f"The exact restate of the same prepared issue is a lock-only correction; got {describe(prepared, row)}")
