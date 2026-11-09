"""Regression guard for restore-approved-locks (outside Done when; passes on main today).

A branch that edited a locked file itself, where the merge with main is clean, is still refused.
"""

import pytest

from runner_support import run_case  # noqa: F401
from foragent.reasons import Reason
from foragent.runner import run_issue

from restore_approved_locks_support import (
    LOCK, ORIGINAL, OTHER, OWN_SOURCE, approve, configure, place, wip_commit,
)


@pytest.mark.parametrize("saved_at", ["origin", "local"])
def test_own_lock_edit_with_clean_merge_is_still_refused(run_case, saved_at):
    c = run_case
    configure(c)
    c.history.update({LOCK: ORIGINAL, OTHER: ORIGINAL, "src/x.py": "X = 0\n"})
    head = wip_commit(c, c.history.tip, {**OWN_SOURCE, LOCK: "# the issue edited its own lock\n"})
    c.history.update({OTHER: "# main corrected another lock\n"})  # LOCK merges cleanly
    place(c, head, saved_at)
    approve(c)
    c.executor.steps = [{".foragent/QUESTION.md": "Must not start."}]
    assert run_issue(1, c.ctx) == "hash-mismatch"
    assert c.store.load_approval(1) is None
    assert c.store.load_flag(1).reason == Reason.HASH_MISMATCH
    assert "approved" not in c.tracker.get_issue(1).labels
    assert c.executor.calls == []
