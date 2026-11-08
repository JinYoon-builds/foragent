"""Guards for restate-generated-parts: regression controls that already hold on main (not in Done when).

- An authored change of a prepared issue still needs reviewed tests (and lands nothing).
- An edit cannot author the generated parts: Preparation notes text written into an authored section
  is an authored change.
- A correction of a prepared issue that its proofs reject still does not land.
- The existing lock-only routes keep working on bodies without generated parts.
"""

import pytest

from lock_corrections_beside_case import (BRANCH, CLOSED, FIXED_TEST, HELPER, LOCKED, NEW_CLOSED,  # noqa: F401
                                          NEW_HELPER, NUMBER, OLD_CLOSED, OLD_HELPER, accepted, apply, case,
                                          describe, offline_publication, on_origin, publish_and_check, world)
from restate_generated_case import (correction_from_seed, generated_parts, nothing_prepared,  # noqa: F401
                                    prepared, seed)

REVIEWED_REFUSAL = "no reviewed tests"


def _changed(seeded, field):
    edit = dict(seeded)
    if field == "goal":
        edit["goal"] = seeded["goal"] + " Gears count twice."
    elif field == "statement":
        edit["statements"] = [seeded["statements"][0] + " Gears count twice."]
    elif field == "files":
        edit["files"] = ["src/gear.py"]
    elif field == "done_when":
        edit["done_when"] = [*seeded["done_when"], f"uv run pytest -q {CLOSED}"]
    else:  # The edit writes the generated section itself into its goal.
        edit["goal"] = seeded["goal"] + generated_parts().split("\n<!--")[0]
    return edit


@pytest.mark.parametrize("field", ["goal", "statement", "files", "done_when", "authored_notes"])
def test_authored_change_of_a_prepared_issue_needs_reviewed_tests(prepared, monkeypatch, capsys, field):
    seeded = seed(prepared, monkeypatch, capsys)
    plan = correction_from_seed(_changed(seeded, field), {HELPER: NEW_HELPER}, plan_id=f"2026-11-09-guard-{field}")
    row = publish_and_check(prepared, plan)
    assert row is None and prepared.refusal is not None and REVIEWED_REFUSAL in prepared.refusal, (
        f"An edit changing the {field} of a prepared issue needs reviewed tests; got {describe(prepared, row)}")
    assert on_origin(prepared, "main", HELPER) == OLD_HELPER
    assert prepared.tracker.get_issue(NUMBER).body == prepared.prepared_body, "A refused edit writes nothing"


def test_prepared_issue_helper_correction_failing_on_the_branch_does_not_land(prepared, monkeypatch, capsys):
    """Wrong value: the corrected helper does not make #7's locked test pass on the issue branch."""
    prepared.outcomes[("branch", LOCKED, "locked", "new")] = False
    plan = correction_from_seed(seed(prepared, monkeypatch, capsys), {HELPER: NEW_HELPER})
    row = publish_and_check(prepared, plan)
    assert not accepted(row), describe(prepared, row)
    if row is not None:
        apply(prepared, plan)
    assert on_origin(prepared, "main", HELPER) == OLD_HELPER


def test_prepared_issue_closed_lock_correction_failing_on_the_branch_does_not_land(prepared, monkeypatch, capsys):
    """Wrong value: the corrected closed-issue lock does not pass on the issue branch."""
    prepared.outcomes[("branch", CLOSED, "new", "old")] = False
    plan = correction_from_seed(seed(prepared, monkeypatch, capsys), {CLOSED: NEW_CLOSED})
    row = publish_and_check(prepared, plan)
    assert not accepted(row), describe(prepared, row)
    if row is not None:
        apply(prepared, plan)
    assert on_origin(prepared, "main", CLOSED) == OLD_CLOSED
    assert on_origin(prepared, BRANCH, CLOSED) == OLD_CLOSED


@pytest.mark.parametrize("name, data", [(LOCKED, FIXED_TEST), (HELPER, NEW_HELPER)])
def test_lock_only_routes_without_generated_parts_still_publish(world, monkeypatch, capsys, name, data):
    """#2558 (own lock) and #2841 (beside helper) on a body without generated parts, seeded the same way."""
    started = nothing_prepared(monkeypatch)
    plan = correction_from_seed(seed(world, monkeypatch, capsys), {name: data})
    row = publish_and_check(world, plan)
    assert accepted(row), describe(world, row)
    row = apply(world, plan)
    assert row["status"] == "published", describe(world, row)
    assert on_origin(world, "main", name) == data
    assert started == []


def test_closed_lock_route_without_generated_parts_still_lands_on_the_branch(world, monkeypatch, capsys):
    plan = correction_from_seed(seed(world, monkeypatch, capsys), {CLOSED: NEW_CLOSED})
    row = publish_and_check(world, plan)
    assert row is not None and row["status"] != "refused", describe(world, row)
    apply(world, plan)
    assert on_origin(world, "main", CLOSED) == OLD_CLOSED
    assert on_origin(world, BRANCH, CLOSED) == NEW_CLOSED
