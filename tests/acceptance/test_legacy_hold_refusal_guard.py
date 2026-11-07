"""Regression guard for legacy-hold-releases-on-edit, statement 2.

A same-class hold set before #2458 has no recorded hold contract. Today `fa orch retry N`
refuses it whatever happens, so this statement already holds on main: these tests are an
existing-behaviour guard, not red evidence. They keep the release for approved Goal edits
(test_legacy_hold_releases_on_edit.py) from turning into "a missing contract releases":
with no approved change since the hold, plain or as a same-content re-approval, the retry
is still refused with the 'narrow, split or redesign' message and has no effect.
"""

import pytest

from foragent.reasons import Reason
from test_legacy_hold_releases_on_edit import approve_edit, legacy_held_case, says_how_to_proceed
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.mark.parametrize("reapproved", [False, True], ids=["no-reapproval", "same-content-reapproval"])
def test_legacy_hold_without_an_approved_change_stays_refused(
        git_history, tmp_path, monkeypatch, capsys, kicks, reapproved):
    case, main = legacy_held_case(git_history, tmp_path, monkeypatch)
    if reapproved:
        approve_edit(case, case.store.load_approval(1).body)
    labels = sorted(case.tracker.get_issue(1).labels)
    capsys.readouterr()
    kicked = list(kicks)

    code = main(["orch", "retry", "1"])
    out = capsys.readouterr()

    assert code not in (0, None), "A legacy hold with no approved change must still refuse the retry"
    assert says_how_to_proceed(out.out + out.err), (
        f"The refusal must say to narrow, split or redesign the Goal through an order edit: {out!r}")
    assert "queued implementation retry" not in out.out + out.err
    assert sorted(case.tracker.get_issue(1).labels) == labels, "A refused retry must not change labels"
    assert kicks == kicked, "A refused retry must not kick a gate tick"
    assert case.store.load_flag(1).reason == Reason.REVIEW_DEFECTS, "The hold stays as it was"
