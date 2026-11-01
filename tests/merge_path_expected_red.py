"""Unlocked strict-xfail registry. Each owning issue removes only its own line."""

import pytest

# Fault proofs for the locked file (#1509, 2026-11-01, base 28909cab). Each ran one
# test in a scratch copy of the repository, with EXPECTED_RED emptied. Line numbers
# are the line of the locked file that failed.
# a 344: _turn_order returns join order -> merge order [5, 1, 4, 2].
# b 344: base refresh uses `-s ours` -> 1, 4, 2 end retries-exceeded.
# b 380: base refresh removes src/neighbor.py -> all merge, main loses the file.
# c 344: require_comparison ignores lost tests -> the incompatible 5 merges.
# c 361: the comparison's failure text changes -> rework loses the reason.
# d 401: the same ignored lost tests -> the regression merges. On base 403 is red;
#        comparing before await_turn turns the whole test green.
# e 344: comparison rejects reports whose exclusions shrank -> all four end held.
#        The same fault leaves test_a green, so the closure is what triggers it.
# e 424: an empty commit published after the closure -> head 5 changes.
# f 465: an uninstallable dependency in pyproject -> the offline tool install fails.
# g 533: no overlap work in _overlap turns it green; 300,000 loop steps there fail.
# g 534: that repair with "Shared member" removed from the frame -> not rendered.
# h 544: group_limit returns 1 -> no combined CI PR. h 545: no close_pr -> PR left open.
#        On base 551 is red; no scratch repair was built for it.
# With this list intact: a KeyError in status.collect fails test_g (not an xfail), and
# a green listed case ends in [XPASS(strict)] (test_g; test_e when added to the list).
# Known gap, not fixable here: the client assertion at 484 cannot fail. Moving merge_pr
# to a helper thread leaves test_f green: the recording tracker's own merge_pr frame is
# always kept. Scratch only: a src/foragent filter plus requiring a frame other than
# tracker.py passes unfaulted and fails on that thread fault.

EXPECTED_RED = {
    "test_d_lost_comparison_never_joins_queue": "I1/d: comparison is consumed after ready is appended",
    "test_g_twenty_shared_file_runs_watch_once_has_bounded_work": "I1/g: overlap work exceeds the deterministic watch budget",
    "test_h_one_ci_round_covers_multiple_member_merges": "I1/h: followers refresh after the combined CI pass",
}


def expected_red(name):
    reason = EXPECTED_RED.get(name)
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason) if reason else lambda test: test
