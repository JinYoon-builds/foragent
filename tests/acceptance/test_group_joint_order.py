"""Locked acceptance for group-joint. Do not edit during implementation.

Break-it: checking each PR alone, omitting a member's checks, using stale main,
or ignoring the configured limit fails these observations of real runner calls.
The imported helpers supply only the existing offline test environment.
"""

from collections import Counter
import json
import shlex

import pytest

from foragent.config import load_config
from test_merge_group import (  # noqa: F401
    contend, group_case, isolated_home, offline_acceptance, prepare, publish_case,
)
from test_base_move_reuse import MERGED, UNOWNED


def joint_members(case):
    """Observe check contents, independent of how group evidence is stored."""
    return [row for row in case.probes if row.armed and row.before_merges == 0
            and len(row.present) > 1 and "tools/group_probe.py" in row.command]


def test_consecutive_fifo_heads_share_one_complete_check_on_current_main(group_case):
    case = group_case
    prepare(case, (5, 1, 4))
    case.move_on_ready = True
    contend(case)
    rows = joint_members(case)
    assert rows, "Missing joint check: ready FIFO heads were checked only separately"
    assert Counter(int(shlex.split(row.command)[-1]) for row in rows) == Counter(case.members)
    assert all(row.present == frozenset(case.members) for row in rows)
    assert all(row.epoch == "EPOCH = 1\n" for row in rows), "Joint check must include current main"
    assert all(row.result.exit_code == 0 and not row.result.timed_out for row in rows)
    union = [row for row in case.probes if row.armed and row.before_merges == 0
             and row.present == frozenset(case.members)]
    for selector in (f"{MERGED}::test_merged", f"{UNOWNED}::test_unowned"):
        assert sum(selector in shlex.split(row.command) for row in union) == 1, selector
    for n in case.members:
        path = f"tests/test_member_{n}.py"
        assert sum(path in shlex.split(row.command) for row in union) == 1, path
    assert all(row.holder is None for row in union), "Checks must leave the merge lock available"
    assert [row.issue for row in case.merges] == list(case.members)
    assert all(case.outcomes[n] == "merged" for n in case.members)
    assert getattr(load_config(case.root, global_path=case.root / "absent.yml"),
                   "merge_group_max", None) == "auto"  # absent key: auto, which starts at 4


def test_explicit_maximum_counts_the_holder_and_one_disables_grouping(group_case):
    case = group_case
    prepare(case, (5, 1, 4), maximum=2)
    contend(case)
    rows = joint_members(case)
    assert rows, "merge_group_max=2 must actually check two ready FIFO heads together"
    assert {row.present for row in rows} == {frozenset((5, 1))}, (
        "Use the first consecutive FIFO turns, including the holder, within the limit"
    )
    assert [row.issue for row in case.merges] == [5, 1, 4]
    # Exercise off=1 in a second, fresh set of runs in the same offline history.
    case.armed = False
    case.probes.clear()
    case.merges.clear()
    prepare(case, (60, 70), maximum=1)
    contend(case)
    assert joint_members(case) == []
    assert [row.issue for row in case.merges] == [60, 70]
    done = [row for row in case.probes if "tools/group_probe.py" in row.command]
    assert Counter(row.actor for row in done) == Counter({60: 1, 70: 2}), (
        "The one-PR path must still check a result-affecting predecessor merge"
    )
    assert all(case.outcomes[n] == "merged" for n in (60, 70))
    assert getattr(load_config(case.root, global_path=case.root / "absent.yml"),
                   "merge_group_max", None) == 1


def test_setting_accepts_unbounded_positive_integers_only(group_case):
    case = group_case
    for value in (1, 4, 1024):
        (case.root / ".foragent.yml").write_text(f"repo: owner/repo\nmerge_group_max: {value}\n")
        config = load_config(case.root, global_path=case.root / "absent.yml")
        assert getattr(config, "merge_group_max", None) == value
    for value in (0, -1, True, 1.5, "two"):
        (case.root / ".foragent.yml").write_text(
            f"repo: owner/repo\nmerge_group_max: {json.dumps(value)}\n")
        with pytest.raises(ValueError, match="merge_group_max"):
            load_config(case.root, global_path=case.root / "absent.yml")
