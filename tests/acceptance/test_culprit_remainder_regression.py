"""Regression guards for culprit-remainder (not in Done when).

This case already passes on main (reviewer hold b, 2026-11-07); it stays
unlocked here so that the change keeps it true:
- a lone remaining member whose PR CI did not run on the current main is not
  landed from the retired train's result.
"""

from pathlib import Path
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from helpers.merge_world import world  # noqa: E402, F401
from test_merge_owner_proto import proto  # noqa: E402, F401
import test_merge_owner_proto as support  # noqa: E402
from culprit_remainder_world import (  # noqa: E402, F401
    CULPRIT, build_train, culprit_at_final_check, landed, on_main,
    set_own_ci, trains,
)


def test_single_remaining_member_without_green_pr_ci_on_current_main_is_not_landed_from_old_train(
        trains, monkeypatch):
    case = trains
    train = build_train(case, [5, CULPRIT])
    set_own_ci(case, 5, "pending")  # #5 was verified only inside the retired train
    culprit_at_final_check(case, monkeypatch, train, CULPRIT)

    for _ in range(3):
        support.gate(case)
    assert 5 not in landed(case) and not on_main(case, 5), (
        f"The retired train's passing CI is not reused for #5: landed {landed(case)}")

    # Once #5 is verified on the current main, it lands.
    case.pending_batch = False
    for pr in case.tracker.created_prs:
        case.tracker.set_pr_checks(pr.number, "fail" if pr.head == f"foragent/issue-{CULPRIT}" else "pass")
    for _ in range(3):
        support.gate(case)
        if 5 in landed(case):
            break
    assert landed(case).get(5) == 1 and on_main(case, 5), f"#5 lands once after its verification: {landed(case)}"
    assert CULPRIT not in landed(case)
