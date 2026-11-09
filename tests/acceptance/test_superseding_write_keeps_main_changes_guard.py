"""Regression control (outside Done when): a superseding request whose files main did not
change after its snapshot still lands exactly the snapshot's content, in one commit."""

from conftest import git_command
from foragent import main_write_requests as writes
from test_order import offline_publication  # noqa: F401
from test_superseding_write_keeps_main_changes import (  # noqa: F401
    LOCAL, MEMORY, contents, main_tip, owner, owner_pass, pushes, row, superseding_snapshot)


def test_file_main_did_not_change_lands_the_snapshot_content(owner):
    case = owner
    blocked, later = superseding_snapshot(case)
    before, pushed = main_tip(case), pushes(case)

    report = owner_pass(case)

    assert row(case, blocked["id"])["status"] == "retired", report.actions
    assert row(case, later["id"])["status"] == "done", report.actions
    assert contents(case, MEMORY) == LOCAL
    assert len(git_command(case.origin, "rev-list", "--first-parent", f"{before}..main").splitlines()) == 1
    assert pushes(case) - pushed == 1
    assert not writes.pending(case.store)
