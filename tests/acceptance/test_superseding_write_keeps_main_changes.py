"""A superseding main-write request keeps changes main made to its files after its snapshot.

Public entry points only: role-memory snapshots are queued by the ordinary producer
(`orch_role_memory.publish` with the merge owner on) and the queue is drained by one
owner pass (`merge_owner.tick`). Real Git trees run on a local bare origin; the shared
offline transport imports commits and pushes. Assertions read origin's main, the
request queue and the orchestrator inbox.

Timeline of every case: main holds a five-line role memory file; a first snapshot is
blocked by an add/add conflict on another file; the checkout edits the memory file and
queues a later snapshot that supersedes the blocked one; only then does main change the
memory file. The change main got before the later snapshot (the add/add file) is not a
change "after the snapshot" and is left to the locked supersede tests (PM q81 B).

Break-it: writing the snapshot's files as they are (today) erases main's later line and
fails the merge case; skipping the request's own change fails the same exact-content
check; a doubled commit or push fails the counts; applying the overlapping request
silently fails the conflict case's status, content and file-name checks.
"""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import main_write_requests as writes, merge_owner, orch_inbox, orch_role_memory
from foragent.scheduler import TickReport
from foragent.state import StateStore
from test_order import offline_publication  # noqa: F401

NOW = datetime(2026, 11, 9, 9, tzinfo=UTC)
ROLES = ("architect", "docs", "orchestrator", "ux-researcher")
FOLDER = {name: f".claude/agent-memory/fa-{name}" for name in ROLES}
REQUESTER = f"roles {', '.join(ROLES)}"
CONFLICTED = f"{FOLDER['ux-researcher']}/broken_issue_dashboard_merged.md"
MEMORY = f"{FOLDER['architect']}/MEMORY.md"
ORIGINAL = "".join(f"Architect lesson {n}\n" for n in range(1, 6))
LOCAL = ORIGINAL.replace("Architect lesson 1\n", "Architect lesson 1, refined locally\n")
MAIN_OTHER_PART = ORIGINAL.replace("Architect lesson 5\n", "Architect lesson 5, refined on main\n")
MAIN_SAME_PART = ORIGINAL.replace("Architect lesson 1\n", "Architect lesson 1, rewritten on main\n")
BOTH = LOCAL.replace("Architect lesson 5\n", "Architect lesson 5, refined on main\n")


@pytest.fixture
def owner(offline_publication, git_history, tmp_path):
    root, config, tracker, git, calls, origin = offline_publication
    git_history.update({MEMORY: ORIGINAL}, message="Seed architect memory")
    git_command(root, "pull", "--ff-only", "--quiet")
    roles = {name: {"agent": f"fa-{name}", "skills": [], "memory": folder + "/"}
             for name, folder in FOLDER.items()}
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    ctx = SimpleNamespace(repo="owner/repo", repo_root=root,
                          config=replace(config, merge_owner=True, roles=roles),
                          tracker=tracker, git=git, store=store, now=lambda: NOW)
    assert git_command(root, "symbolic-ref", "--short", "HEAD") == ctx.config.base_branch
    assert (root / MEMORY).read_text() == ORIGINAL
    return SimpleNamespace(ctx=ctx, root=root, git=git, store=store, origin=origin,
                           calls=calls, history=git_history)


def write(case, name, data):
    path = case.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)


def publish_role_memory(case):
    known = {row["id"] for row in writes.requests(case.store)}
    orch_role_memory.publish(case.ctx, TickReport())
    new = [row for row in writes.requests(case.store) if row["id"] not in known]
    assert len(new) == 1, f"the role-memory tick must queue one request, got {len(new)}"
    assert new[0]["requester"] == REQUESTER
    return new[0]


def owner_pass(case):
    report = TickReport()
    merge_owner.tick(case.ctx, report)
    return report


def main_tip(case):
    return git_command(case.origin, "rev-parse", "main")


def contents(case, name):
    if name not in git_command(case.origin, "ls-tree", "-r", "--name-only", "main").splitlines():
        return None
    return git_command(case.origin, "show", f"main:{name}") + "\n"


def pushes(case):
    return sum(1 for args in case.calls if args[:1] == ["push"])


def row(case, identity):
    return next(r for r in writes.requests(case.store) if r["id"] == identity)


def superseding_snapshot(case):
    """Block a first snapshot, then queue a later one that edits MEMORY's first line."""
    write(case, CONFLICTED, "Local researcher lesson\n")
    write(case, f"{FOLDER['docs']}/MEMORY.md", "Docs lesson\n")
    write(case, f"{FOLDER['orchestrator']}/MEMORY.md", "Orchestrator lesson\n")
    blocked = publish_role_memory(case)
    case.history.update({CONFLICTED: "Main researcher lesson\n"}, message="Main researcher lesson")
    owner_pass(case)
    assert row(case, blocked["id"])["status"] == "blocked"

    write(case, MEMORY, LOCAL)
    later = publish_role_memory(case)
    assert later["payload"].get("snapshot") and MEMORY in later["payload"]["files"]
    return blocked, later


def test_main_change_to_another_part_after_the_snapshot_survives(owner):
    case = owner
    blocked, later = superseding_snapshot(case)
    case.history.update({MEMORY: MAIN_OTHER_PART}, message="Main refines lesson 5")
    before, pushed = main_tip(case), pushes(case)

    report = owner_pass(case)

    assert row(case, blocked["id"])["status"] == "retired", report.actions
    assert row(case, later["id"])["status"] == "done", report.actions
    assert contents(case, MEMORY) == BOTH, (
        "main must keep its later change to lesson 5 and gain the request's change to lesson 1")
    landed = git_command(case.origin, "rev-list", "--first-parent", f"{before}..main").splitlines()
    assert len(landed) == 1, f"one owner pass must land one commit, got {len(landed)}"
    assert pushes(case) - pushed == 1
    assert not writes.pending(case.store)


def test_overlapping_main_change_blocks_the_superseding_request_naming_the_file(owner):
    case = owner
    blocked, later = superseding_snapshot(case)
    case.history.update({MEMORY: MAIN_SAME_PART}, message="Main rewrites lesson 1")
    before, pushed = main_tip(case), pushes(case)

    report = owner_pass(case)

    stored = row(case, later["id"])
    assert stored["status"] == "blocked", (
        f"an overlapping superseding request must not be applied, got {stored['status']!r}: {report.actions}")
    assert contents(case, MEMORY) == MAIN_SAME_PART, "main must keep its own change"
    assert main_tip(case) == before and pushes(case) == pushed, "nothing may be written to main"
    assert MEMORY in str(stored.get("error", "")), (
        f"the conflict must name the file, got {stored.get('error')!r}")
    shown = [item for item in orch_inbox.requests(case.store) if item.get("main_write") == later["id"]]
    assert len(shown) == 1, "the conflict must be shown once in the orchestrator inbox"
    assert MEMORY in str(shown[0]), f"the shown conflict must name the file, got {shown[0]!r}"
