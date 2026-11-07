"""Superseding by full role-memory snapshots never depends on the order of earlier requests (#2548).

Review cases: a done deletion is never undone by superseding, and blocked snapshots in a
chain are all retired by the next covering snapshot. A later request that does not cover
every file of the blocked one leaves the queue stopped.

Break-it: applying the retired write brings F back; superseding only one blocked row
leaves the second blocked; superseding without full coverage lands the partial request.
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

NOW = datetime(2026, 11, 8, 9, tzinfo=UTC)
ROLES = ("orchestrator", "ux-researcher")
FOLDER = {name: f".claude/agent-memory/fa-{name}" for name in ROLES}
REQUESTER = f"roles {', '.join(ROLES)}"
Z = f"{FOLDER['ux-researcher']}/lesson.md"      # main adds it differently: add/add
F = f"{FOLDER['orchestrator']}/MEMORY.md"
G = f"{FOLDER['orchestrator']}/later.md"


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


def publish(case):
    orch_role_memory.publish(case.ctx, TickReport())
    return writes.requests(case.store)[-1]


def tick(case):
    merge_owner.tick(case.ctx, TickReport())


def statuses(case):
    return [row["status"] for row in writes.requests(case.store)]


def test_superseding_never_undoes_a_done_deletion(case, git_history):
    """A adds F and conflicting Z; B deletes F and lands; C changes G: F stays absent."""
    write(case, Z, b"Local\n")
    write(case, F, b"v1\n")
    publish(case)
    git_history.update({Z: "Main\n"})
    tick(case)
    assert statuses(case) == ["blocked"]

    (case.root / F).unlink()
    publish(case)
    tick(case)
    assert statuses(case) == ["retired", "done"]
    assert show(case, F) is None

    write(case, G, b"g\n")
    publish(case)
    tick(case)
    tick(case)

    assert statuses(case) == ["retired", "done", "done"]
    assert show(case, F) is None, "a deletion that landed must not be undone"
    assert show(case, G) == "g"
    assert show(case, Z) == "Local"


def test_chain_of_blocked_snapshots_lands_the_newest_content(case, git_history):
    """A adds F=v1 and Z; B updates both to v2 before draining; both block; C retires both."""
    write(case, Z, b"Local v1\n")
    write(case, F, b"v1\n")
    publish(case)
    write(case, Z, b"Local v2\n")
    write(case, F, b"v2\n")
    publish(case)
    git_history.update({Z: "Main\n"})
    tick(case)
    assert statuses(case) == ["blocked", "blocked"]

    write(case, G, b"g\n")
    publish(case)
    tick(case)

    assert statuses(case) == ["retired", "retired", "done"]
    assert show(case, F) == "v2"
    assert show(case, Z) == "Local v2"
    assert show(case, G) == "g"
    tip = git_command(case.origin, "rev-parse", "main")
    tick(case)
    assert git_command(case.origin, "rev-parse", "main") == tip and statuses(case)[-1] == "done"


def test_request_that_does_not_cover_the_blocked_files_keeps_the_queue_stopped(case, git_history):
    """Same requester, but a partial (non-snapshot) request: nothing is retired or landed."""
    write(case, Z, b"Local\n")
    write(case, F, b"v1\n")
    publish(case)
    git_history.update({Z: "Main\n"})
    tick(case)
    write(case, G, b"g\n")
    partial = writes.capture(case.ctx.git, case.ctx.config, [G], "Memory", REQUESTER, store=case.store)
    assert partial is not None

    tick(case)

    assert statuses(case) == ["blocked", "pending"]
    assert show(case, G) is None and show(case, F) is None
