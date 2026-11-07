"""A blocked role-memory write is replaced by the next snapshot instead of stopping the queue.

Public entry points only: role-memory requests are queued by the ordinary role-memory
producer (`orch_role_memory.publish` with the merge owner on), other requests by
`main_write_requests.capture`, and the queue is drained by one owner pass
(`merge_owner.tick`). Real Git trees run on a local origin; the shared offline
transport imports commits and pushes. Assertions read origin's main, the request
queue, and the orchestrator inbox.

Break-it: a blocked write that still stops the queue fails the "later write lands"
checks; a blocked write that is dropped without the superseded outcome fails the
status/reason checks; a later write landed twice fails the commit and push counts;
a write of another requester treated as a later snapshot fails the "queue stops" step.
The no-later-snapshot case (queue stops, reported once) is guarded by current tests.
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

NOW = datetime(2026, 11, 7, 9, tzinfo=UTC)
ROLES = ("architect", "docs", "orchestrator", "ux-researcher")
FOLDER = {name: f".claude/agent-memory/fa-{name}" for name in ROLES}
REQUESTER = f"roles {', '.join(ROLES)}"
CONFLICTED = f"{FOLDER['ux-researcher']}/broken_issue_dashboard_merged.md"
ARCHITECT = f"{FOLDER['architect']}/MEMORY.md"
DOCS_NEW = f"{FOLDER['docs']}/later_lesson.md"
LANE = ".claude/agent-memory/fa-api/MEMORY.md"


@pytest.fixture
def owner(offline_publication, tmp_path):
    root, config, tracker, git, calls, origin = offline_publication
    roles = {name: {"agent": f"fa-{name}", "skills": [], "memory": folder + "/"}
             for name, folder in FOLDER.items()}
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    ctx = SimpleNamespace(repo="owner/repo", repo_root=root,
                          config=replace(config, merge_owner=True, roles=roles),
                          tracker=tracker, git=git, store=store, now=lambda: NOW)
    assert git_command(root, "symbolic-ref", "--short", "HEAD") == ctx.config.base_branch
    return SimpleNamespace(ctx=ctx, root=root, git=git, store=store, origin=origin, calls=calls)


def write(case, name, data):
    path = case.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def publish_role_memory(case):
    """The ordinary role-memory tick step; returns the request it queued."""
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
    """File text on origin's main, or None when main lacks the file."""
    if name not in git_command(case.origin, "ls-tree", "-r", "--name-only", "main").splitlines():
        return None
    return git_command(case.origin, "show", f"main:{name}")


def pushes(case):
    return sum(1 for args in case.calls if args[:1] == ["push"])


def row(case, identity):
    return next(r for r in writes.requests(case.store) if r["id"] == identity)


def blocked_role_memory(case, git_history, *, producer="publish"):
    """Queue a role-memory snapshot, then let main add the same file differently (add/add)."""
    write(case, CONFLICTED, b"Local researcher lesson\n")
    write(case, ARCHITECT, b"Architect lesson one\n")
    write(case, f"{FOLDER['docs']}/MEMORY.md", b"Docs lesson\n")
    write(case, f"{FOLDER['orchestrator']}/MEMORY.md", b"Orchestrator lesson\n")
    if producer == "publish":
        blocked = publish_role_memory(case)
    else:
        # A request exactly as today's producer queues it (the stuck r928 write predates the fix).
        blocked = writes.capture(case.git, case.ctx.config, list(FOLDER.values()),
                                 f"Foragent role memory: {', '.join(ROLES)}", REQUESTER, store=case.store)
    git_history.update({CONFLICTED: "Main researcher lesson\n"})
    report = owner_pass(case)
    assert row(case, blocked["id"])["status"] == "blocked", report.actions
    message, = orch_inbox.requests(case.store)
    assert message["main_write"] == blocked["id"]
    return blocked


def assert_superseded(case, blocked):
    retired = row(case, blocked["id"])
    assert retired["status"] == "retired", (
        f"the blocked role-memory write must be retired, got {retired['status']!r}")
    assert "supersed" in str(retired.get("reason", "")).lower(), (
        f"the retirement must say the write was superseded, got {retired.get('reason')!r}")
    assert blocked["id"] not in {r["id"] for r in writes.pending(case.store)}


@pytest.mark.parametrize("producer", ["publish", "legacy"])
def test_later_snapshot_retires_blocked_write_and_lands_in_one_pass(owner, git_history, producer):
    case = owner
    blocked = blocked_role_memory(case, git_history, producer=producer)
    write(case, DOCS_NEW, b"A later docs lesson\n")
    later = publish_role_memory(case)
    assert row(case, blocked["id"])["status"] == "blocked"
    before, pushed = main_tip(case), pushes(case)

    owner_pass(case)

    assert_superseded(case, blocked)
    assert row(case, later["id"])["status"] == "done", "the later role-memory write must land"
    assert contents(case, DOCS_NEW) == "A later docs lesson"
    landed = git_command(case.origin, "rev-list", "--first-parent", f"{before}..main").splitlines()
    assert len(landed) == 1, f"one owner pass must land one commit, got {len(landed)}"
    assert pushes(case) - pushed == 1, "one owner pass must push main exactly once"
    assert contents(case, CONFLICTED) == "Main researcher lesson", "main's side of the conflict stays"
    assert not writes.pending(case.store)

    # Nothing is left: a further pass neither commits, pushes nor reports.
    tip, pushed, messages = main_tip(case), pushes(case), len(orch_inbox.requests(case.store))
    owner_pass(case)
    assert main_tip(case) == tip and pushes(case) == pushed
    assert len(orch_inbox.requests(case.store)) == messages
    assert row(case, blocked["id"])["status"] == "retired"


def test_later_snapshot_carries_newer_content_of_the_blocked_files(owner, git_history):
    """The later snapshot edits a file the blocked write created; its newer content lands."""
    case = owner
    blocked = blocked_role_memory(case, git_history)
    write(case, ARCHITECT, b"Architect lesson one\nArchitect lesson two\n")
    later = publish_role_memory(case)
    before, pushed = main_tip(case), pushes(case)

    owner_pass(case)

    assert_superseded(case, blocked)
    assert row(case, later["id"])["status"] == "done"
    assert contents(case, ARCHITECT) == "Architect lesson one\nArchitect lesson two"
    assert len(git_command(case.origin, "rev-list", "--first-parent", f"{before}..main").splitlines()) == 1
    assert pushes(case) - pushed == 1
    assert not writes.pending(case.store)


def test_queue_continues_past_superseded_write_with_other_requesters_in_order(owner, git_history):
    """A pending write of another requester does not supersede; the later snapshot does, and both land."""
    case = owner
    blocked = blocked_role_memory(case, git_history)
    write(case, LANE, b"Lane lesson\n")
    lane = writes.capture(case.git, case.ctx.config, [LANE], "Memory", "lane api", store=case.store)
    assert lane is not None

    owner_pass(case)  # No later snapshot from the same requester yet: the queue stops.
    assert row(case, blocked["id"])["status"] == "blocked"
    assert row(case, lane["id"])["status"] == "pending"
    assert contents(case, LANE) is None
    assert len(orch_inbox.requests(case.store)) == 1

    write(case, DOCS_NEW, b"A later docs lesson\n")
    later = publish_role_memory(case)
    pushed = pushes(case)
    owner_pass(case)

    assert_superseded(case, blocked)
    assert row(case, lane["id"])["status"] == "done"
    assert row(case, later["id"])["status"] == "done"
    assert contents(case, LANE) == "Lane lesson"
    assert contents(case, DOCS_NEW) == "A later docs lesson"
    assert pushes(case) - pushed == 1
    assert not writes.pending(case.store)

