"""A superseded role-memory snapshot hands its non-conflicting files to the later one (#2548).

The later snapshot is captured against the blocked one, so it holds only what changed
since. Files the blocked write alone carried must still land; only the conflicting file
keeps main's side.

Break-it: drop the carried files and the orchestrator lesson never reaches main.
"""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import main_write_requests as writes, merge_owner, orch_role_memory
from foragent.scheduler import TickReport
from foragent.state import StateStore
from test_order import offline_publication  # noqa: F401

NOW = datetime(2026, 11, 7, 9, tzinfo=UTC)
ROLES = ("orchestrator", "ux-researcher")
FOLDER = {name: f".claude/agent-memory/fa-{name}" for name in ROLES}
CONFLICTED = f"{FOLDER['ux-researcher']}/lesson.md"
CARRIED = f"{FOLDER['orchestrator']}/MEMORY.md"
LATER = f"{FOLDER['orchestrator']}/later.md"


@pytest.fixture
def case(offline_publication, tmp_path):
    root, config, tracker, git, calls, origin = offline_publication
    roles = {name: {"agent": f"fa-{name}", "skills": [], "memory": folder + "/"}
             for name, folder in FOLDER.items()}
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    ctx = SimpleNamespace(repo="owner/repo", repo_root=root,
                          config=replace(config, merge_owner=True, roles=roles),
                          tracker=tracker, git=git, store=store, now=lambda: NOW)
    return SimpleNamespace(ctx=ctx, root=root, store=store, origin=origin)


def write(case, name, data):
    path = case.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def show(case, name):
    if name not in git_command(case.origin, "ls-tree", "-r", "--name-only", "main").splitlines():
        return None
    return git_command(case.origin, "show", f"main:{name}")


def test_superseded_snapshot_carries_its_non_conflicting_files(case, git_history):
    write(case, CONFLICTED, b"Local\n")
    write(case, CARRIED, b"Orchestrator lesson\n")
    orch_role_memory.publish(case.ctx, TickReport())
    blocked, = writes.pending(case.store)
    git_history.update({CONFLICTED: "Main\n"})
    merge_owner.tick(case.ctx, TickReport())
    assert writes.requests(case.store)[0]["status"] == "blocked"

    write(case, LATER, b"Later lesson\n")
    orch_role_memory.publish(case.ctx, TickReport())
    merge_owner.tick(case.ctx, TickReport())

    statuses = {row["id"]: row["status"] for row in writes.requests(case.store)}
    assert statuses.pop(blocked["id"]) == "retired"
    assert set(statuses.values()) == {"done"}
    assert show(case, LATER) == "Later lesson"
    assert show(case, CARRIED) == "Orchestrator lesson"
    assert show(case, CONFLICTED) == "Main"


def test_chain_of_blocked_snapshots_lands_the_newest_content(case, git_history):
    """A adds F=v1 with a conflicting file, B updates F=v2 before draining: both block; v2 lands."""
    write(case, CONFLICTED, b"Local\n")
    write(case, CARRIED, b"v1\n")
    orch_role_memory.publish(case.ctx, TickReport())
    write(case, CARRIED, b"v2\n")
    orch_role_memory.publish(case.ctx, TickReport())
    first, second = writes.pending(case.store)
    git_history.update({CONFLICTED: "Main\n"})
    merge_owner.tick(case.ctx, TickReport())
    assert {row["status"] for row in writes.requests(case.store)} == {"blocked"}

    write(case, LATER, b"Later lesson\n")
    orch_role_memory.publish(case.ctx, TickReport())
    merge_owner.tick(case.ctx, TickReport())

    assert show(case, CARRIED) == "v2", "the newest snapshot of F must reach main"
    assert show(case, LATER) == "Later lesson"
    assert show(case, CONFLICTED) == "Main"
    assert next(r for r in writes.requests(case.store) if r["id"] == first["id"])["status"] == "retired"
    assert not writes.pending(case.store)


def test_request_between_snapshots_on_the_same_file_keeps_the_queue_stopped(case, git_history):
    """Folding past another request that changed the same file would land stale bytes; stop instead."""
    write(case, CONFLICTED, b"Local\n")
    write(case, CARRIED, b"v1\n")
    orch_role_memory.publish(case.ctx, TickReport())
    git_history.update({CONFLICTED: "Main\n"})
    merge_owner.tick(case.ctx, TickReport())
    blocked, = writes.pending(case.store)
    write(case, CARRIED, b"v2\n")
    between = writes.capture(case.ctx.git, case.ctx.config, [FOLDER["orchestrator"]], "Memory",
                             "roles orchestrator", store=case.store)
    assert between is not None
    write(case, LATER, b"Later lesson\n")
    orch_role_memory.publish(case.ctx, TickReport())

    merge_owner.tick(case.ctx, TickReport())

    assert next(r for r in writes.requests(case.store) if r["id"] == blocked["id"])["status"] == "blocked"
    assert show(case, CARRIED) is None, "no stale or partial content of F may land"


def test_blocked_next_snapshot_retries_once_it_receives_the_base(case, git_history):
    """With no third snapshot, B (blocked only for lack of A's base) lands after folding A in."""
    write(case, CONFLICTED, b"Local\n")
    write(case, CARRIED, b"v1\n")
    orch_role_memory.publish(case.ctx, TickReport())
    write(case, CARRIED, b"v2\n")
    orch_role_memory.publish(case.ctx, TickReport())
    git_history.update({CONFLICTED: "Main\n"})
    merge_owner.tick(case.ctx, TickReport())
    assert {row["status"] for row in writes.requests(case.store)} == {"blocked"}

    merge_owner.tick(case.ctx, TickReport())

    assert [row["status"] for row in writes.requests(case.store)] == ["retired", "done"]
    assert show(case, CARRIED) == "v2"
    assert show(case, CONFLICTED) == "Main"
