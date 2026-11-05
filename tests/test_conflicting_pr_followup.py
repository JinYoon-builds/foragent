"""#2035 follow-up: a stale CONFLICTING answer never silences a later real conflict.

Reuses the locked conflicting-pr-rework world (real runner, Git, merge owner; fake GitHub).
"""

from foragent.reasons import Reason  # noqa: F401
from acceptance.test_merge_path_real_settings import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
from acceptance.test_conflicting_pr_rework import (  # noqa: F401
    BRANCH, N, OWN, RESOLVED, conflicting, conflicts_with_main, contains, git, log, pushes, run,
)

SECOND_MAIN = "VALUE = 'a second lane owns this'\n"


def test_stale_conflicting_answer_then_a_real_conflict_refreshes_again(conflicting, monkeypatch):
    case = conflicting
    measured = case.ci.reports

    def stale_reports(row):
        # While GitHub still answers CONFLICTING for the resolved head, it starts no CI for it.
        earlier = [old for old in case.ci.by_head.values() if old.branch == row.branch]
        return [] if row.branch == BRANCH and len(earlier) == 1 else measured(row)

    monkeypatch.setattr(case.ci, "reports", stale_reports)
    real = case.mergeable
    state = {"stale": 0, "second": None}

    def mergeable(head):
        # After the first resolution GitHub keeps answering CONFLICTING for a head that
        # already contains main (stale), until main really moves again.
        rounds = pushes(case)
        if state["second"] is None and len(rounds) == 2 and head == rounds[1].head:
            state["stale"] += 1
            return "CONFLICTING"
        return real(head)

    case.mergeable = mergeable
    sleep = case.ctx.sleep

    def moving_sleep(seconds):
        sleep(seconds)
        if state["second"] is None and state["stale"] >= 3:
            case.history.update({OWN: SECOND_MAIN}, message="A second lane owns this file")
            state["second"] = case.history.tip

    case.ctx.sleep = moving_sleep

    outcome = run(case)

    assert state["stale"] >= 3 and state["second"] is not None, "Setup: stale answers, then a second move"
    rounds = pushes(case)
    assert outcome == "merge-queued", f"got {outcome!r}, flag {case.store.load_flag(N)}\n{log(case)[-2000:]}"
    assert case.store.load_flag(N) is None
    assert case.resolutions == 2, f"Each real conflict is resolved once, got {case.resolutions}"
    assert len(rounds) == 3 and case.branch_pushes == 3, [row.head for row in rounds]
    final = rounds[2]
    assert contains(case, state["second"], final.head) and not conflicts_with_main(case, final.head)
    assert contains(case, rounds[1].head, final.head)
    assert git(case, "show", f"{final.head}:{OWN}") == RESOLVED.strip()
    assert final.reports, "CI measures the head that merged the second move"
    assert "which the head already contains; waiting" in log(case)
