"""Acceptance tests for restore-approved-locks (architect r1447).

A retry's branch holds a WIP commit with an older version of a locked test; main changed that
lock twice and the issue is approved with main's bytes. The start's base merge conflicts on the
lock; it must take the approved bytes and start implementation instead of ending hash-mismatch.
"""

import pytest

from conftest import git_command
from runner_support import run_case  # noqa: F401
from foragent.reasons import Reason
from foragent.runner import run_issue

from restore_approved_locks_support import (
    APPROVED, BRANCH, CORRECTED_1, LOCK, LOCKS, ORIGINAL, OTHER, OWN_SOURCE, WIP_COPY,
    approve, configure, place, wip_commit,
)


def build(case, saved_at, *, source_conflict=False):
    configure(case)
    case.history.update({LOCK: ORIGINAL, OTHER: ORIGINAL, "src/x.py": "X = 0\n"})
    fork = case.history.tip
    own = {**OWN_SOURCE, LOCK: WIP_COPY}  # the preserve step saved the lock approved back then
    if source_conflict:
        own["src/x.py"] = "X = 'branch'\n"
    head = wip_commit(case, fork, own)
    case.history.update({LOCK: CORRECTED_1})  # first test owner correction
    later = {LOCK: APPROVED}  # second correction
    if source_conflict:
        later["src/x.py"] = "X = 'main'\n"
    case.history.update(later)
    place(case, head, saved_at)
    approve(case)
    # The base merge really conflicts on the lock (the case under test).
    assert git_command(case.history.origin, "rev-list", "--count", f"{fork}..{head}") == "1"
    assert len(git_command(case.history.origin, "rev-list", "--parents", "-n", "1", head).split()) == 2
    assert case.ctx.git.blob_shas(head, list(LOCKS))[LOCK] != case.store.load_approval(1).acceptance_blobs[LOCK]
    case.executor.steps = [{".foragent/QUESTION.md": "Fixture stopped after the start."}]
    return head


def start_and_observe(case, monkeypatch):
    seen = []
    execute = case.executor.run

    def observe(**kwargs):
        tree = kwargs["worktree"]
        seen.append({path: (tree / path).read_bytes() for path in LOCKS}
                    | {"own": (tree / "src/widget.py").read_text()})
        return execute(**kwargs)

    monkeypatch.setattr(case.executor, "run", observe)
    return run_issue(1, case.ctx), seen


@pytest.mark.parametrize("saved_at", ["origin", "local"])
@pytest.mark.parametrize("source_conflict", [False, True], ids=["lock-only", "with-source-conflict"])
def test_retry_with_older_wip_lock_starts_and_keeps_approval(run_case, monkeypatch, saved_at, source_conflict):
    build(run_case, saved_at, source_conflict=source_conflict)
    approved_blobs = dict(run_case.store.load_approval(1).acceptance_blobs)
    outcome, seen = start_and_observe(run_case, monkeypatch)
    flag = run_case.store.load_flag(1)
    assert outcome != "hash-mismatch", "An older WIP lock with an approval equal to main must not stop the start"
    assert flag is None or flag.reason != Reason.HASH_MISMATCH, flag
    assert outcome == "spec-question", (outcome, flag)
    assert len(seen) == 1, "Implementation (or the resolver) starts exactly once"
    approval = run_case.store.load_approval(1)
    assert approval is not None, "The approval must survive the start"
    assert approval.acceptance_blobs == approved_blobs
    assert "approved" in run_case.tracker.get_issue(1).labels
    assert seen[0]["own"] == OWN_SOURCE["src/widget.py"], "The branch's own work is kept"


@pytest.mark.parametrize("saved_at", ["origin", "local"])
@pytest.mark.parametrize("source_conflict", [False, True], ids=["lock-only", "with-source-conflict"])
def test_locked_file_in_worktree_has_exactly_the_approved_bytes(run_case, monkeypatch, saved_at, source_conflict):
    build(run_case, saved_at, source_conflict=source_conflict)
    outcome, seen = start_and_observe(run_case, monkeypatch)
    assert seen, f"The start never reached the worker; outcome={outcome}, flag={run_case.store.load_flag(1)}"
    assert seen[0][LOCK] == APPROVED.encode(), "The conflicted lock must hold exactly the approved bytes"
    assert seen[0][OTHER] == ORIGINAL.encode()
    blob = run_case.store.load_approval(1).acceptance_blobs[LOCK]
    assert git_command(run_case.history.origin, "cat-file", "blob", blob) == APPROVED.strip()
