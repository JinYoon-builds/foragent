"""A superseding main write without a recorded main fails closed (#2878 r1287 follow-up).

Public entry points only: role-memory snapshots are queued by the ordinary producer
(`orch_role_memory.publish` with the merge owner on), other requests by
`main_write_requests.capture`, and the queue is drained by owner passes
(`merge_owner.tick`). Real Git trees run on a local bare origin (the existing
superseding-write world); no network. Assertions read origin's main, the request
queue as `fa gate status` shows it (`main_write_requests.summary`) and the pass report.

Timeline: main holds a five-line role memory file; a first snapshot is blocked by an
add/add conflict on another file; the checkout edits the memory file's first line and
queues a later snapshot that supersedes the blocked one, but that snapshot records no
main, because (a) the capture's fetch of main failed, or (b) the request is a legacy
one queued before #2878 recorded main. Only then does main edit the file's last line.

Break-it: applying the snapshot as today (writing its files as they are) erases main's
line 5 and lands the request (content and status checks fail); dropping the reason
fails the reason check; stopping the FIFO at the held request fails the later-request
checks; landing a later request twice fails the commit and push counts.
"""

import json

import pytest

from conftest import git_command
from foragent import main_write_requests as writes
from foragent.git import GitError
from test_order import offline_publication  # noqa: F401
from test_superseding_write_keeps_main_changes import (  # noqa: F401
    CONFLICTED, FOLDER, LOCAL, MAIN_OTHER_PART, MEMORY, contents, main_tip, owner, owner_pass,
    publish_role_memory, pushes, row, write)

LATER_FILE = "notes/after-the-snapshot.md"
LATER_TEXT = "Queued after the superseding snapshot\n"
LAST_FILE = "notes/after-the-hold.md"
LAST_TEXT = "Queued after the hold\n"


def failing_fetch(case):
    def fetch(remote="origin", ref=None):
        raise GitError("fatal: unable to access 'origin': Could not resolve host")
    return fetch


def strip_recorded_main(case, identity):
    """Make the stored request look like one queued before #2878 recorded main."""
    path = case.store.root / "main-writes" / f"{identity}.json"
    data = json.loads(path.read_text())
    data["payload"].pop("main", None)
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n")
    return data


def unrecorded_superseding_snapshot(case, how, monkeypatch):
    """Block a first snapshot, then queue a later full snapshot that records no main."""
    write(case, CONFLICTED, "Local researcher lesson\n")
    write(case, f"{FOLDER['docs']}/MEMORY.md", "Docs lesson\n")
    write(case, f"{FOLDER['orchestrator']}/MEMORY.md", "Orchestrator lesson\n")
    blocked = publish_role_memory(case)
    case.history.update({CONFLICTED: "Main researcher lesson\n"}, message="Main researcher lesson")
    owner_pass(case)
    assert row(case, blocked["id"])["status"] == "blocked"

    write(case, MEMORY, LOCAL)
    if how == "fetch fails":
        with monkeypatch.context() as patch:
            patch.setattr(case.git, "fetch", failing_fetch(case))
            later = publish_role_memory(case)
    else:
        later = publish_role_memory(case)
        later = strip_recorded_main(case, later["id"])
    stored = row(case, later["id"])
    assert stored["payload"].get("snapshot") and MEMORY in stored["payload"]["files"]
    assert stored["payload"].get("main") is None, "precondition: the request records no main"
    return blocked, later


def queue_other(case, name, text, requester):
    write(case, name, text)
    request = writes.capture(case.git, case.ctx.config, [name], f"Write {name}", requester,
                             store=case.store)
    assert request is not None
    return request


def shown(case, identity):
    return next((r for r in writes.summary(case.store) if r["id"] == identity), None)


def assert_held_with_reason(case, later, report):
    stored = row(case, later["id"])
    assert stored["status"] != "done", (
        f"a superseding request without a recorded main must not land over main's later edit: "
        f"{report.actions}")
    assert contents(case, MEMORY) == MAIN_OTHER_PART, (
        "main must keep its later edit of lesson 5 and must not receive the request's content")
    view = shown(case, later["id"])
    assert view is not None, "the held request must stay visible in the main-write queue"
    reason = view.get("error") or view.get("reason")
    assert isinstance(reason, str) and reason.strip(), f"the held request must say why, got {view!r}"
    mentioned = [action for action in report.actions if later["id"] in action]
    assert mentioned and not any(": applied" in action for action in mentioned), (
        f"the pass must report the request as held, not applied: {report.actions}")


@pytest.mark.parametrize("how", ["fetch fails", "legacy"])
def test_superseding_request_without_recorded_main_is_held_with_its_reason(owner, how, monkeypatch):
    case = owner
    blocked, later = unrecorded_superseding_snapshot(case, how, monkeypatch)
    case.history.update({MEMORY: MAIN_OTHER_PART}, message="Main refines lesson 5")
    before, pushed = main_tip(case), pushes(case)

    report = owner_pass(case)

    assert_held_with_reason(case, later, report)
    assert main_tip(case) == before and pushes(case) == pushed, "nothing may be written to main"


@pytest.mark.parametrize("how", ["fetch fails", "legacy"])
def test_requests_queued_after_a_held_superseding_request_still_land(owner, how, monkeypatch):
    case = owner
    blocked, later = unrecorded_superseding_snapshot(case, how, monkeypatch)
    queued = queue_other(case, LATER_FILE, LATER_TEXT, "docs")
    case.history.update({MEMORY: MAIN_OTHER_PART}, message="Main refines lesson 5")
    before, pushed = main_tip(case), pushes(case)

    report = owner_pass(case)

    assert_held_with_reason(case, later, report)
    assert row(case, queued["id"])["status"] == "done", (
        f"a request queued behind the held one must land in the same pass: {report.actions}")
    assert contents(case, LATER_FILE) == LATER_TEXT
    landed = git_command(case.origin, "rev-list", "--first-parent", f"{before}..main").splitlines()
    assert len(landed) == 1, f"one owner pass must land one commit, got {len(landed)}"
    assert pushes(case) - pushed == 1

    # The held request must not stall the queue on later passes either.
    last = queue_other(case, LAST_FILE, LAST_TEXT, "architect")
    before, pushed = main_tip(case), pushes(case)

    report = owner_pass(case)

    assert row(case, last["id"])["status"] == "done", (
        f"a request queued after the hold must land on the next pass: {report.actions}")
    assert contents(case, LAST_FILE) == LAST_TEXT
    assert contents(case, MEMORY) == MAIN_OTHER_PART, "the held request must still not land"
    assert row(case, later["id"])["status"] != "done"
    landed = git_command(case.origin, "rev-list", "--first-parent", f"{before}..main").splitlines()
    assert len(landed) == 1 and pushes(case) - pushed == 1
