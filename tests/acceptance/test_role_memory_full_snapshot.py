"""Role-memory requests are full role-folder snapshots (PM q81 B, #2548).

Public entry points only: role-memory requests are queued by the ordinary role-memory
producer (`orch_role_memory.publish` with the merge owner on) and the queue is drained by
one owner pass (`merge_owner.tick`). Real Git trees run on a local origin; the shared
offline transport imports commits and pushes. Assertions read origin's main and the request
queue only.

Break-it: a later snapshot that skips a deletion leaves the removed file on main; landing
the blocked write's content (or main's side) instead of the later snapshot's fails the
content checks; a superseded write applied again resurrects a deleted file; landing twice
fails the commit and push counts; a covered blocked write left behind fails the
"nothing blocked or pending" checks.
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
ROLES = ("architect", "docs", "orchestrator", "ux-researcher")
FOLDER = {name: f".claude/agent-memory/fa-{name}" for name in ROLES}
REQUESTER = f"roles {', '.join(ROLES)}"
CONFLICTED = f"{FOLDER['ux-researcher']}/broken_issue_dashboard_merged.md"
ARCHITECT = f"{FOLDER['architect']}/MEMORY.md"
DOCS = f"{FOLDER['docs']}/MEMORY.md"
ORCHESTRATOR = f"{FOLDER['orchestrator']}/MEMORY.md"
ON_MAIN = f"{FOLDER['architect']}/old_lesson.md"        # tracked on main before the incident
BLOCKED_ONLY = f"{FOLDER['orchestrator']}/draft.md"     # created only by the blocked write
DOCS_NEW = f"{FOLDER['docs']}/later_lesson.md"


@pytest.fixture
def owner(offline_publication, git_history, tmp_path):
    root, config, tracker, git, calls, origin = offline_publication
    # Main already tracks one role-memory file; the checkout matches main.
    git_history.update({ON_MAIN: "Old architect lesson\n"}, message="Role memory on main")
    git_command(root, "fetch", "origin")
    git_command(root, "merge", "--ff-only", "origin/main")
    roles = {name: {"agent": f"fa-{name}", "skills": [], "memory": folder + "/"}
             for name, folder in FOLDER.items()}
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    ctx = SimpleNamespace(repo="owner/repo", repo_root=root,
                          config=replace(config, merge_owner=True, roles=roles),
                          tracker=tracker, git=git, store=store, now=lambda: NOW)
    assert git_command(root, "symbolic-ref", "--short", "HEAD") == ctx.config.base_branch
    assert (root / ON_MAIN).read_text() == "Old architect lesson\n"
    return SimpleNamespace(ctx=ctx, root=root, git=git, store=store, origin=origin, calls=calls,
                           history=git_history)


def write(case, name, data):
    path = case.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def remove(case, name):
    (case.root / name).unlink()


def publish_role_memory(case):
    """The ordinary role-memory tick step; returns the request it queued."""
    known = {r["id"] for r in writes.requests(case.store)}
    orch_role_memory.publish(case.ctx, TickReport())
    new = [r for r in writes.requests(case.store) if r["id"] not in known]
    assert len(new) == 1, f"the role-memory tick must queue one request, got {len(new)}"
    assert new[0]["requester"] == REQUESTER
    return new[0]


def owner_pass(case):
    report = TickReport()
    merge_owner.tick(case.ctx, report)
    return report


def main_tip(case):
    return git_command(case.origin, "rev-parse", "main")


def main_files(case):
    return set(git_command(case.origin, "ls-tree", "-r", "--name-only", "main").splitlines())


def contents(case, name):
    """File text on origin's main, or None when main lacks the file."""
    if name not in main_files(case):
        return None
    return git_command(case.origin, "show", f"main:{name}")


def pushes(case):
    return sum(1 for args in case.calls if args[:1] == ["push"])


def row(case, identity):
    return next(r for r in writes.requests(case.store) if r["id"] == identity)


def local_folder_files(case):
    return {p.relative_to(case.root).as_posix() for folder in FOLDER.values()
            for p in (case.root / folder).rglob("*") if p.is_file()}


def blocked_role_memory(case):
    """Queue a role-memory request, then let main add one of its files differently (add/add)."""
    write(case, CONFLICTED, b"Local researcher lesson\n")
    write(case, ARCHITECT, b"Architect lesson one\n")
    write(case, ON_MAIN, b"Old architect lesson\nEdited before the incident\n")
    write(case, DOCS, b"Docs lesson\n")
    write(case, ORCHESTRATOR, b"Orchestrator lesson\n")
    write(case, BLOCKED_ONLY, b"Orchestrator draft\n")
    blocked = publish_role_memory(case)
    case.history.update({CONFLICTED: "Main researcher lesson\n"})
    report = owner_pass(case)
    assert row(case, blocked["id"])["status"] == "blocked", report.actions
    return blocked


def assert_superseded(case, blocked):
    retired = row(case, blocked["id"])
    assert retired["status"] == "retired", (
        f"the blocked role-memory write must be retired, got {retired['status']!r}")
    assert "supersed" in str(retired.get("reason", "")).lower(), (
        f"the retirement must say the write was superseded, got {retired.get('reason')!r}")


def assert_nothing_left(case):
    left = [(r["id"], r["status"]) for r in writes.requests(case.store)
            if r["status"] not in writes.TERMINAL_STATUSES]
    assert not left, f"no role-memory write may stay blocked or pending, got {left}"


def assert_main_is_snapshot(case):
    """Every role-memory file on main equals the checkout; nothing removed locally is on main."""
    local = local_folder_files(case)
    on_main = {name for name in main_files(case) if name.startswith(".claude/agent-memory/")}
    assert on_main == local, (
        f"main must hold exactly the later snapshot: extra {sorted(on_main - local)}, "
        f"missing {sorted(local - on_main)}")
    for name in local:
        assert contents(case, name) == (case.root / name).read_text().rstrip("\n"), name


def test_later_snapshot_lands_its_deletions_and_newer_content(owner):
    case = owner
    blocked = blocked_role_memory(case)
    remove(case, ON_MAIN)        # tracked on main, removed by the later snapshot
    remove(case, BLOCKED_ONLY)   # only ever in the blocked write
    write(case, ARCHITECT, b"Architect lesson one\nArchitect lesson two\n")
    later = publish_role_memory(case)
    assert row(case, blocked["id"])["status"] == "blocked"
    before, pushed = main_tip(case), pushes(case)

    owner_pass(case)

    assert_superseded(case, blocked)
    assert row(case, later["id"])["status"] == "done", "the later role-memory write must land"
    assert contents(case, ON_MAIN) is None, "a file the later snapshot removed must be absent on main"
    assert contents(case, BLOCKED_ONLY) is None, "the superseded write's removed file must not land"
    assert contents(case, ARCHITECT) == "Architect lesson one\nArchitect lesson two"
    assert contents(case, CONFLICTED) == "Local researcher lesson", "the later snapshot's content lands"
    assert contents(case, DOCS) == "Docs lesson"
    assert contents(case, ORCHESTRATOR) == "Orchestrator lesson"
    assert_main_is_snapshot(case)
    landed = git_command(case.origin, "rev-list", "--first-parent", f"{before}..main").splitlines()
    assert len(landed) == 1, f"one owner pass must land one commit, got {len(landed)}"
    assert pushes(case) - pushed == 1, "one owner pass must push main exactly once"
    assert_nothing_left(case)


def test_later_snapshot_request_carries_the_whole_role_folder(owner):
    """A one-file edit still queues every file of every named role folder, removals included."""
    case = owner
    blocked = blocked_role_memory(case)
    remove(case, ON_MAIN)
    write(case, DOCS_NEW, b"A later docs lesson\n")
    later = publish_role_memory(case)

    files = later["payload"].get("files", {})
    expected = local_folder_files(case) | {ON_MAIN}
    missing = sorted(expected - set(files))
    assert not missing, f"the role-memory request must be a full folder snapshot; missing {missing}"
    assert files[ON_MAIN]["after"] is None, "a file removed since main is carried as a removal"
    assert set(row(case, blocked["id"])["payload"]["files"]) <= set(files), (
        "the later snapshot covers every file of the blocked write")


def test_superseding_never_resurrects_a_file_a_later_done_write_deleted(owner):
    case = owner
    blocked = blocked_role_memory(case)
    remove(case, ON_MAIN)
    remove(case, BLOCKED_ONLY)
    deleting = publish_role_memory(case)
    owner_pass(case)
    assert_superseded(case, blocked)
    assert row(case, deleting["id"])["status"] == "done"
    assert contents(case, ON_MAIN) is None and contents(case, BLOCKED_ONLY) is None

    # Later snapshots and passes never bring back what the done write deleted.
    write(case, DOCS_NEW, b"A later docs lesson\n")
    next_one = publish_role_memory(case)
    pushed = pushes(case)
    owner_pass(case)
    assert row(case, next_one["id"])["status"] == "done"
    assert pushes(case) - pushed == 1
    assert contents(case, DOCS_NEW) == "A later docs lesson"
    assert contents(case, ON_MAIN) is None, "a deleted file must not be resurrected"
    assert contents(case, BLOCKED_ONLY) is None, "the superseded write must never be applied"
    assert row(case, blocked["id"])["status"] == "retired"
    assert_main_is_snapshot(case)
    assert_nothing_left(case)

    tip, pushed = main_tip(case), pushes(case)
    owner_pass(case)
    assert main_tip(case) == tip and pushes(case) == pushed
    assert contents(case, ON_MAIN) is None


def test_several_later_snapshots_leave_no_covered_write_behind(owner):
    """Blocked, then two later snapshots: one pass ends with the newest snapshot on main."""
    case = owner
    blocked = blocked_role_memory(case)
    write(case, ARCHITECT, b"Architect lesson two\n")
    remove(case, BLOCKED_ONLY)
    second = publish_role_memory(case)
    write(case, DOCS_NEW, b"A later docs lesson\n")
    remove(case, ON_MAIN)
    third = publish_role_memory(case)
    before, pushed = main_tip(case), pushes(case)

    owner_pass(case)

    assert_superseded(case, blocked)
    assert row(case, third["id"])["status"] == "done", "the newest snapshot must land"
    assert row(case, second["id"])["status"] in writes.TERMINAL_STATUSES
    assert_nothing_left(case)
    assert contents(case, ARCHITECT) == "Architect lesson two"
    assert contents(case, DOCS_NEW) == "A later docs lesson"
    assert contents(case, CONFLICTED) == "Local researcher lesson"
    assert contents(case, ON_MAIN) is None and contents(case, BLOCKED_ONLY) is None
    assert_main_is_snapshot(case)
    assert len(git_command(case.origin, "rev-list", "--first-parent", f"{before}..main").splitlines()) == 1
    assert pushes(case) - pushed == 1
