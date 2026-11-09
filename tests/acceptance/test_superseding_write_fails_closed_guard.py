"""Regression controls (outside Done when) for the fail-closed superseding write.

A superseding request that records its main still merges main's later edit (#2878),
and the request queued behind it lands in the same pass.
"""

from foragent import main_write_requests as writes
from test_order import offline_publication  # noqa: F401
from test_superseding_write_fails_closed import LATER_FILE, LATER_TEXT, queue_other
from test_superseding_write_keeps_main_changes import (  # noqa: F401
    MAIN_OTHER_PART, MEMORY, contents, owner, owner_pass, row, superseding_snapshot)

BOTH = MAIN_OTHER_PART.replace("Architect lesson 1\n", "Architect lesson 1, refined locally\n")


def test_superseding_request_with_recorded_main_still_merges(owner):
    case = owner
    blocked, later = superseding_snapshot(case)
    assert later["payload"].get("main")
    queued = queue_other(case, LATER_FILE, LATER_TEXT, "docs")
    case.history.update({MEMORY: MAIN_OTHER_PART}, message="Main refines lesson 5")

    report = owner_pass(case)

    assert row(case, later["id"])["status"] == "done", report.actions
    assert row(case, queued["id"])["status"] == "done", report.actions
    assert contents(case, MEMORY) == BOTH
    assert contents(case, LATER_FILE) == LATER_TEXT
    assert not writes.pending(case.store)
