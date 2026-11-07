"""Acceptance (culprit-remainder): after a train's final check names a culprit,
the rest lands without a full new train when it was already verified.

r1039 (2026-11-07): #2161 alone failed the final check of batch #2512, and
#2448 then waited about 30 minutes for a whole new train CI. After this issue,
a remaining set that equals something already verified on the current main
(for example one member whose PR CI ran on the current main SHA) lands at once;
otherwise only the new combination is verified, and the old train's result is
never reused.

World: tests/acceptance/culprit_remainder_world.py (real gate tick, merge owner,
merge train, Git and state; fake GitHub, models and clock). After the culprit
is named, every newly started train CI stays pending, so a landing in the
following passes cannot come from a new train CI run. The culprit's red CI
comes from the fake GitHub check transport at the owner's final recheck, not
from any lock (review hold d).
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
    CULPRIT, OUTSIDER, build_train, culprit_at_final_check, groups, landed, new_train_members, on_main,
    queue_workers, remaining_member_lands_without_new_train_ci, set_own_ci, trains,
)


def test_only_remaining_member_alone_lands_without_new_train_ci(trains, monkeypatch):
    remaining_member_lands_without_new_train_ci(trains, monkeypatch, outsider=False)


def test_only_remaining_member_with_green_pr_ci_on_current_main_lands_without_new_train_ci(trains, monkeypatch):
    # Another PR is approved and queued while the train runs; its own CI stays
    # pending. The "no green PR CI" guard already passes on main and lives in
    # test_culprit_remainder_regression.py.
    remaining_member_lands_without_new_train_ci(trains, monkeypatch, outsider=True)


def test_remaining_set_never_verified_together_is_verified_alone_before_it_lands(trains, monkeypatch):
    case = trains
    train = build_train(case, [5, CULPRIT, 4])
    queue_workers(case, [OUTSIDER])
    for n in (5, 4):
        set_own_ci(case, n, "pending")  # {5, 4} was never verified on the current main
    before = culprit_at_final_check(case, monkeypatch, train, CULPRIT)

    for _ in range(3):
        support.gate(case)
    assert not landed(case) and not any(on_main(case, n) for n in (5, 4, CULPRIT, OUTSIDER)), (
        f"The old train's passing CI is not reused for the remaining set: landed {landed(case)}")
    built = new_train_members(case, before)
    assert built, "the owner starts verifying the remaining set"
    assert sorted(built[0]) == [4, 5], (
        f"Only the new combination {{4, 5}} is verified, without other queued PRs: {built}")

    # The verification of the remaining set passes; then the set lands.
    case.pending_batch = False
    for pr in groups(case)[before:]:
        case.tracker.set_pr_checks(pr.number, "pass")
    for _ in range(3):
        support.gate(case)
        if {4, 5} <= set(landed(case)):
            break
    assert landed(case).get(5) == 1 and landed(case).get(4) == 1, f"the verified set lands once: {landed(case)}"
    assert on_main(case, 5) and on_main(case, 4)
    assert CULPRIT not in landed(case) and not on_main(case, CULPRIT)
    assert OUTSIDER not in landed(case), "a PR outside the verified set does not land with it"
