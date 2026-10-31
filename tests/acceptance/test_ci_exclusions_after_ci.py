"""Acceptance test for ci-exclusions-reference. Locked by Foragent; do not edit."""

from pathlib import Path

import pytest

from foragent.ci_mode import UnusableComparison, comparison
from foragent.ci_workflow import measurement_scope
from foragent.config import Config
from test_ci_mode import EXCLUDED, FUTURE, KEEP, LOCKED, OTHER
from test_night_ci_evidence import EXPECTED_BASE, EXPECTED_HEAD, good_report


NEW_LOCKED = "tests/acceptance/test_order_added_after_ci.py"


def recorded_evidence():
    """Reuse trusted, complete two-shard evidence from the existing CI tests."""
    values = [good_report(0, count=2), good_report(1, count=2)]
    for value, collected, passing in zip(values, ([KEEP, FUTURE], [OTHER]), ([KEEP], [OTHER])):
        payload = value["report"]
        payload["excluded"] = list(EXCLUDED)
        payload["scope"] = measurement_scope(Config(ci_shards=2), Path("."), EXCLUDED)
        for side in ("base", "head"):
            payload[side] = {
                "complete": True,
                "collected_ids": list(collected),
                "passing_ids": list(passing),
            }
    return values


def compare_with_current_locks(values, current, *, scope_exclusions=None):
    return comparison(
        values, "owner/repo", EXPECTED_BASE, EXPECTED_HEAD, current,
        expected_scope=measurement_scope(
            Config(ci_shards=2), Path("."), current if scope_exclusions is None else scope_exclusions,
        ),
        expected_shards=range(2),
    )


@pytest.mark.parametrize("add_new_lock", [False, True], ids=["current-shrank", "current-removed-and-added"])
@pytest.mark.parametrize("reference", ["both-fields", "scope-only"])
def test_ci_exclusions_removed_from_current_locks_leave_comparison_usable_and_unblocked(add_new_lock, reference):
    values = recorded_evidence()
    current = [path for path in EXCLUDED if path != LOCKED]
    if add_new_lock:
        current = sorted([*current, NEW_LOCKED])

    # LOCKED's order merged after CI, so its file left today's exclusions.
    # Both report.excluded and report.scope.excluded still record that file.
    # CI measured neither LOCKED nor the later NEW_LOCKED: neither acquires
    # retroactive protection or creates a blocker in this completed comparison.
    # Keep the report reference historical in the scope-only variant to
    # independently detect a current-list subset restriction in the scope.
    report_reference = current if reference == "both-fields" else list(EXCLUDED)
    assert compare_with_current_locks(values, report_reference, scope_exclusions=current) == ([], 3)


@pytest.mark.parametrize("head_result", ["failed", "deselected"])
def test_test_not_excluded_at_ci_still_blocks_even_if_it_is_locked_now(head_result):
    values = recorded_evidence()
    current = sorted([*EXCLUDED, NEW_LOCKED, KEEP.split("::", 1)[0]])
    head = values[0]["report"]["head"]
    head["passing_ids"] = []
    if head_result == "deselected":
        head["collected_ids"].remove(KEEP)

    # KEEP passed at CI's base and was not excluded then. A later approval
    # must not waive its measured regression. FUTURE already failed on base.
    lost, duration = compare_with_current_locks(values, current)
    assert lost == [KEEP], "A test measured by CI lost its passing status; later locks cannot waive it"
    assert duration == 3


@pytest.mark.parametrize("regressed,expected", [(False, []), (True, [KEEP])], ids=["passing", "regression"])
def test_identical_exclusions_keep_existing_comparison_results(regressed, expected):
    values = recorded_evidence()
    if regressed:
        values[0]["report"]["head"]["passing_ids"] = []

    # Matching exclusions keep healthy evidence usable, still detect a real
    # regression, and do not promote the already-failing FUTURE to a blocker.
    assert compare_with_current_locks(values, list(EXCLUDED)) == (expected, 3)


def test_report_and_scope_exclusions_must_still_agree():
    values = recorded_evidence()
    current = sorted([*EXCLUDED, NEW_LOCKED])
    values[0]["report"]["scope"]["excluded"] = [path for path in EXCLUDED if path != LOCKED]

    # Historical exclusions are authoritative, but contradictory recorded
    # fields remain invalid even when each list is a subset of today's locks.
    with pytest.raises(UnusableComparison):
        compare_with_current_locks(values, current)
