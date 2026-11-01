"""Unlocked strict-xfail registry. Each owning issue removes only its own line."""

import pytest

EXPECTED_RED = {
    "test_d_lost_comparison_never_joins_queue": "I1/d: comparison is consumed after ready is appended",
    "test_g_twenty_shared_file_runs_watch_once_has_bounded_work": "I1/g: overlap work exceeds the deterministic watch budget",
    "test_h_one_ci_round_covers_multiple_member_merges": "I1/h: followers refresh after the combined CI pass",
}


def expected_red(name):
    reason = EXPECTED_RED.get(name)
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason) if reason else lambda test: test
