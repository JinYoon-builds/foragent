"""Unlocked strict-xfail registry. Each owning issue removes only its own line."""

import pytest

# Fault proofs for the locked file (#1509, base f1f10cbb). Each ran one test in a
# scratch copy of the repository, with EXPECTED_RED emptied. Numbers are the line of
# the locked file that failed.
# a 345: _turn_order returns join order -> merge order [5, 1, 4, 2].
# a 350: the runner skips record_merge -> "Persist the runner's successful merge".
# b 345: base refresh uses `-s ours` -> 1, 4, 2 end retries-exceeded.
# b 381: base refresh removes src/neighbor.py -> all merge, main loses the file.
# c 345: require_comparison ignores lost tests -> the incompatible 5 merges.
# c 362: the comparison's failure text changes -> rework loses the reason.
# c 394: lost tests ignored except for 5, refresh breaks src/value.py -> main is red.
# d 402: the same ignored lost tests -> the regression merges. No fault: 404 is red.
#        Comparing before await_turn turns the whole test green.
# e 345: comparison rejects reports whose exclusions shrank -> all four end held.
#        The same fault leaves test_a green, so the closure is what triggers it.
# e 424: an empty commit published after the closure -> head 5 changes.
# f 484: merge_pr in a helper thread, for all members or for member 1 alone.
# f 463: an uninstallable dependency in pyproject -> the offline tool install fails.
# g 534: 300,000 loop steps in _overlap -> over budget. No fault: 534 is red.
# g 535: _overlap skipped and "Shared member" removed from the frame -> not rendered.
#        Known trap: skipping _overlap alone also stops at 535. Work is 176,913, but
#        since #1062 the frame shows "#101 merge" and no titles. The owner of the g
#        line needs that check updated by the test owner, or titles in the frame.
# h 545: group_limit returns 1 -> no combined CI PR. h 546: no close_pr -> PR left open.
#        No fault: 552 is red. No scratch repair was built for it.
# With this list intact: a KeyError in status.collect fails test_g (not an xfail), and
# the d repair ends in [XPASS(strict)]. test_e added to the list does the same.

EXPECTED_RED = {
    "test_d_lost_comparison_never_joins_queue": "I1/d: comparison is consumed after ready is appended",
    "test_g_twenty_shared_file_runs_watch_once_has_bounded_work": "I1/g: overlap work exceeds the deterministic watch budget",
    "test_h_one_ci_round_covers_multiple_member_merges": "I1/h: followers refresh after the combined CI pass",
}


def expected_red(name):
    reason = EXPECTED_RED.get(name)
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason) if reason else lambda test: test
