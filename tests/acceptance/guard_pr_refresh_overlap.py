"""Regression controls for pr-refresh-on-overlap-only (outside Done when).

Statement 2: when main moves with a commit that changes a file the PR changes,
the PR is refreshed onto the new base as today. These pass on current main and
must keep passing. Run with:
    uv run pytest -q tests/acceptance/guard_pr_refresh_overlap.py
"""

from __future__ import annotations

import pr_overlap_refresh_world as scene  # noqa: I001 -- also puts tests/ on sys.path
from helpers.merge_world import world  # noqa: F401 -- fixture used by proto
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from conftest import git_command
from foragent import merge_owner

N = scene.N


def assert_refreshed_once_then_merged(case, admitted, pushes, rounds):
    support.gate(case)
    assert not case.merges, "An overlapping main move merged before the refreshed head's CI"
    assert len(pushes) == 1 and pushes[0] != admitted["head"], (
        f"The PR must be refreshed onto the new base exactly once:\n{scene.log_tail(case)}")
    refreshed = pushes[0]
    assert len(case.ci.rounds) == rounds + 1
    assert case.ci.rounds[-1].head == refreshed
    assert case.ci.rounds[-1].base == case.history.tip
    # The refreshed head is built on the new main and keeps the PR's change.
    tip = case.history.tip
    assert git_command(case.history.origin, "merge-base", "--is-ancestor", tip, refreshed) == ""
    shared = git_command(case.history.origin, "show", f"{refreshed}:{scene.SHARED}").splitlines()
    assert shared[0] == "FIRST = 'pr'"
    assert merge_owner.pending(case.store, N)
    support.gate(case)
    assert [merge["head"] for merge in case.merges] == [refreshed]
    assert len(case.ci.rounds) == rounds + 1, "The refreshed head was refreshed again"
    assert not merge_owner.pending(case.store, N)
    return refreshed


def test_main_move_on_a_file_the_pr_changes_refreshes_the_pr(proto, monkeypatch):
    case = proto
    admitted, pushes, rounds = scene.admitted_pr(case, monkeypatch)
    case.history.update(scene.main_shared("one"))
    assert_refreshed_once_then_merged(case, admitted, pushes, rounds)
    lines = scene.main_file(case, scene.SHARED).splitlines()
    assert lines[0] == "FIRST = 'pr'" and lines[-1] == "LAST = 'main one'"


def test_overlap_between_non_overlapping_commits_still_refreshes(proto, monkeypatch):
    """One overlapping commit among several others is enough to refresh."""
    case = proto
    admitted, pushes, rounds = scene.admitted_pr(case, monkeypatch)
    case.history.update({"src/neighbor.py": "VALUE = 1\n"})
    case.history.update(scene.main_shared("two"))
    case.history.update({"src/neighbor.py": "VALUE = 2\n"})
    assert_refreshed_once_then_merged(case, admitted, pushes, rounds)
    assert scene.main_file(case, "src/neighbor.py") == "VALUE = 2"
    assert scene.main_file(case, scene.SHARED).splitlines()[-1] == "LAST = 'main two'"
