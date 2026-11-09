"""Acceptance tests for pr-refresh-on-overlap-only (Jin r1313).

Statement 1: when main moves with commits that change only files an open issue
PR does not change, the PR is not refreshed onto the new base and its earlier
verification still counts: no push, no new CI round, and the admitted head
itself merges.

Statement 2 (overlap still refreshes as today) passes on current main, so its
regression controls live in guard_pr_refresh_overlap.py, outside Done when.

World: pr_overlap_refresh_world (merge-owner world, local Git with a bare
origin, fake tracker, no network).
"""

from __future__ import annotations

import pr_overlap_refresh_world as scene  # noqa: I001 -- also puts tests/ on sys.path
from helpers.merge_world import world  # noqa: F401 -- fixture used by proto
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import merge_owner

N = scene.N


def assert_kept_and_merged(case, admitted, pushes, rounds):
    """One owner tick merges the admitted, already verified head unchanged."""
    support.gate(case)
    assert pushes == [], (
        "A main move that changed no file of the PR pushed the PR again "
        f"(refreshed onto the new base):\n{scene.log_tail(case)}")
    assert len(case.ci.rounds) == rounds, "A non-overlapping main move started another CI round"
    assert [merge["head"] for merge in case.merges] == [admitted["head"]], (
        f"The admitted head must merge as it is:\n{scene.log_tail(case)}")
    assert case.merges[0]["pr"] == admitted["pr"]
    assert not merge_owner.pending(case.store, N)
    assert case.tracker.get_issue(N).state == "closed"


def test_main_move_on_another_source_file_keeps_the_verified_pr(proto, monkeypatch):
    case = proto
    admitted, pushes, rounds = scene.admitted_pr(case, monkeypatch)
    case.history.update({"src/neighbor.py": "VALUE = 'main moved'\n"})
    moved_tip = case.history.tip
    assert_kept_and_merged(case, admitted, pushes, rounds)
    # The merge lands on the moved main: main keeps its move and gains the PR.
    assert scene.main_file(case, "src/neighbor.py") == "VALUE = 'main moved'"
    assert scene.main_file(case, f"src/member_{N}.py") == f"VALUE = {N}"
    assert scene.main_file(case, scene.SHARED).splitlines()[0] == "FIRST = 'pr'"
    assert case.merges[0]["main"] != moved_tip


def test_several_non_overlapping_commits_keep_the_verified_pr(proto, monkeypatch):
    case = proto
    admitted, pushes, rounds = scene.admitted_pr(case, monkeypatch)
    case.history.update({"src/neighbor.py": "VALUE = 1\n"})
    case.history.update({"tests/test_neighbor.py": "def test_neighbor():\n    assert True\n"})
    case.history.update({"docs/note.md": "note\n", "src/neighbor.py": "VALUE = 2\n"})
    assert_kept_and_merged(case, admitted, pushes, rounds)
    assert scene.main_file(case, "src/neighbor.py") == "VALUE = 2"
    assert scene.main_file(case, "docs/note.md") == "note"
    assert scene.main_file(case, f"src/member_{N}.py") == f"VALUE = {N}"
