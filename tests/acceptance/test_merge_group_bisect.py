"""Acceptance test for merge-group-bisect. Locked by Foragent; do not edit."""

from collections import Counter
import shlex

import pytest

from test_merge_group import (  # noqa: F401
    assert_facts, assert_group_short_holds, assert_members_merged, assert_suite,
    contend, group_case, group_rows, isolated_home, merge_order,
    offline_acceptance, prepare, publish_case, short_case,
)


@pytest.mark.parametrize("members,culprits,parts,extra", [
    pytest.param((1, 4, 5), (1,), [(1,), (4, 5)], 2, id="three-culprit-first"),
    pytest.param((1, 4, 5), (5,), [(1,), (4, 5), (4,), (5,), (1, 4)], 5,
                 id="three-culprit-last-odd-split"),
    pytest.param((1, 4, 5, 6), (1,), [(1, 4), (1,), (4,), (5, 6), (4, 5, 6)], 5,
                 id="four-culprit-first"),
    pytest.param((1, 4, 5, 6), (6,), [(1, 4), (5, 6), (5,), (6,), (1, 4, 5)], 5,
                 id="four-culprit-last"),
    pytest.param((1, 4, 5, 6), (1, 6), [(1, 4), (1,), (4,), (5, 6), (5,), (6,), (4, 5)], 7,
                 id="two-culprits-in-different-halves"),
])
def test_bisect_isolates_culprits_and_counts_every_extra_check(
    group_case, members, culprits, parts, extra,
):
    case = group_case
    prepare(case, members, culprits=culprits)
    contend(case)
    initial = group_rows(case, members)
    assert_suite(initial, members, passed=False)
    survivors = tuple(n for n in members if n not in culprits)
    # Restrict to the leader's diagnosis before its first merge: resumed
    # culprits may subsequently check their own failed base-move result.
    diagnostic = [row for row in case.probes if row.armed and row.before_merges == 0]
    assert all(row.epoch == "EPOCH = 1\n" for row in diagnostic), "Every part must include the same moved main"
    expected = Counter(frozenset(part) for part in [members, *parts])
    observed = Counter(row.present for row in diagnostic
                       if "tools/group_probe.py" in row.command
                       and int(shlex.split(row.command)[-1]) == max(row.present))
    assert observed == expected, (
        f"Bisect must check the ordered halves and verify recombined survivors; "
        f"expected={expected}, observed={observed}"
    )
    assert sum(observed.values()) - 1 == extra
    for part in parts:
        assert_suite([row for row in diagnostic if row.present == frozenset(part)], part,
                     passed=not bool(set(part) & set(culprits)))
    assert merge_order(case) == list(survivors), "Never merge a culprit, including first/last members"
    assert_members_merged(case, survivors)
    for number in survivors:
        # Only the verified survivor union is merged together.
        assert_facts(case, number, len(survivors), extra)
    for number in culprits:
        assert case.outcomes[number] == "retries-exceeded"
        assert case.store.get_run(number).outcome == "retries-exceeded"
        assert case.tracker.get_issue(number).state == "open"
        assert case.store.get_run(number).attempts == 1
        assert f"base-move culprit: {number}" in case.store.log_path(number).read_text()
        assert "blocked" in case.tracker.get_issue(number).labels
    assert len(case.implementer.calls) == len(case.reviewer.calls) == len(members)
    assert_group_short_holds(case)
