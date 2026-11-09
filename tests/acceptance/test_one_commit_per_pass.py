"""Acceptance tests for one-commit-per-pass.

One merge-owner pass writes the neutral non-PR requests it lands (an order
publication, a role memory write and a note) as one commit on main. A request
that cannot land in this pass stays queued with a recorded reason and does not
block the others, which still share one commit.

Public entry points: `order.publish` (publication request), the off-owner
publication check `order_publications.check_pending`, `main_write_requests.capture`
(memory), `order_notes.append` + `order_flush.flush` (note) and the owner pass
`merge_owner.tick`. Real Git trees run on a local bare origin through the
existing offline transport (`offline_publication`); GitHub and measured test runs
are the fakes of tests/test_order_publications.py. No network, model or signal.

Break-it: a publication commit plus a separate memory/notes commit (today) fails
the one-commit and one-push counts; a skipped request fails the content checks;
a doubled commit or push fails the counts; a failing request that aborts the pass
fails the "other two land" checks; a dropped or reasonless failure fails the
"stays queued with a reason" checks.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import main_write_requests as writes, merge_owner, order_flush, order_notes, order_prepare
from foragent import order_prepare_check, order_publications
from foragent.scheduler import TickReport
from test_order import offline_publication  # noqa: F401  (fixture)
from test_order_publications import NAME, case, review_plan, submit  # noqa: F401  (fixture)

MEMORY = ".claude/agent-memory/fa-docs/MEMORY.md"
DECISIONS = "docs/decisions.md"
NOTE = "Retained one-pass decision"
SUPPORT = "tests/acceptance/one_pass_support.py"


@pytest.fixture
def owner(case, offline_publication, git_history):
    """The publication case on a real local origin, with one owner context."""
    root, _config, _tracker, git, calls, origin = offline_publication
    case.ctx.git, case.ctx.repo_root = git, root
    return SimpleNamespace(case=case, ctx=case.ctx, root=root, git=git, calls=calls, origin=origin,
                           store=case.store, history=git_history)


def queue_publication(o):
    submit(o.case)
    order_publications.check_pending(o.ctx, TickReport())
    row, = order_publications.pending(o.store)
    return row


def queue_memory(o, data=b"Docs lesson from one pass\n"):
    path = o.root / MEMORY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    row = writes.capture(o.git, o.ctx.config, [MEMORY], "Memory", "lane docs", store=o.store)
    assert row is not None
    return row


def queue_note(o):
    order_notes.append(o.store, kind="decision", boundary="chosen", content=NOTE)
    order_flush.flush(o.root, o.ctx.config, o.ctx.tracker, o.git, o.store, repo=o.ctx.repo, boundary="chosen")


def tip(o):
    return git_command(o.origin, "rev-parse", "main")


def files(o):
    return git_command(o.origin, "ls-tree", "-r", "--name-only", "main").splitlines()


def contents(o, name):
    return git_command(o.origin, "show", f"main:{name}") if name in files(o) else None


def pushes(o):
    return sum(1 for args in o.calls if args[:1] == ["push"])


def commits_since(o, before):
    return git_command(o.origin, "rev-list", "--first-parent", f"{before}..main").splitlines()


def owner_pass(o):
    report = TickReport()
    merge_owner.tick(o.ctx, report)
    return report


def publication(o, identity):
    return next(row for row in order_publications.requests(o.store) if row["id"] == identity)


def memory(o, identity):
    return next(row for row in writes.requests(o.store) if row["id"] == identity)


def note_written(o):
    text = contents(o, DECISIONS)
    return text is not None and NOTE in text


def publication_written(o, row):
    return all(contents(o, name) == data.rstrip("\n") for name, data in row["files"].items())


# Statement 1 ---------------------------------------------------------------------------

def test_publication_memory_and_note_land_in_one_owner_pass_as_one_commit(owner):
    o = owner
    pub = queue_publication(o)
    mem = queue_memory(o)
    queue_note(o)
    assert len(writes.pending(o.store)) == 2, "memory and note are queued for the owner"
    assert not publication_written(o, pub) and contents(o, MEMORY) is None and not note_written(o)
    before, pushed = tip(o), pushes(o)

    owner_pass(o)

    landed = commits_since(o, before)
    assert len(landed) == 1, (
        f"one owner pass must write the publication, memory and note as one commit on main, got {len(landed)}")
    assert git_command(o.origin, "rev-parse", "main^") == before
    assert pushes(o) - pushed == 1, "one owner pass must push main once for its non-PR requests"
    assert publication_written(o, pub), "the publication's files must be in the commit"
    assert contents(o, MEMORY) == "Docs lesson from one pass"
    assert note_written(o), "the note must be in the commit"
    assert publication(o, pub["id"])["status"] == "published"
    assert memory(o, mem["id"])["status"] == "done"
    assert not writes.pending(o.store) and not order_publications.pending(o.store)
    # A one-issue order publishes its parent issue and that one child (stale since the parent/child
    # publication shape; #2506 spec question 11-09): each exists once.
    issues = len(o.case.tracker.list_issues())
    assert issues == 2, "the publication's parent issue and its one child issue each exist once"

    # Nothing is left: a second pass moves main no further.
    after, pushed = tip(o), pushes(o)
    owner_pass(o)
    assert tip(o) == after and pushes(o) == pushed
    assert len(o.case.tracker.list_issues()) == issues


# Statement 2 ---------------------------------------------------------------------------

def no_setup(o):
    pass


def block_memory(o, pub, mem):
    """Main changes the memory file differently after the request was captured."""
    o.history.update({MEMORY: "Different main lesson\n"})
    # The move is neutral for the publication's tests; renew its check on the new main.
    order_publications.check_pending(o.ctx, TickReport())
    return "memory"


def import_support(o):
    """The publication's locked test imports a sibling module that exists on main."""
    o.history.update({SUPPORT: "VALUE = 1\n"})
    o.case.plan["issues"][0]["acceptance_files"][NAME] = (
        "from one_pass_support import VALUE\n\n\ndef test_new():\n    assert VALUE == 2\n")
    path = order_prepare._record_path(o.store, o.case.plan)
    record = order_prepare._read(path, o.case.plan)
    record["issues"]["new"]["written"] = deepcopy(o.case.plan["issues"][0]["acceptance_files"])
    order_prepare._write(path, record)
    review_plan(o.case)


def block_publication(o, pub, mem):
    """Main moves in a module the publication's test imports; its red check cannot be renewed now."""
    assert "one_pass_support" in pub["files"][NAME]
    o.history.update({SUPPORT: "VALUE = 2\n"})

    def unavailable(*args, **kwargs):
        raise OSError("test runner unavailable in this pass")

    # The move touches the test's imports, so its certification must be re-run; no run can finish now.
    o.monkeypatch.setattr(order_prepare_check, "run", unavailable)
    return "publication"


@pytest.mark.parametrize(("setup", "blocked"), [(no_setup, block_memory), (import_support, block_publication)],
                         ids=["memory", "publication"])
def test_one_request_that_cannot_land_stays_queued_and_the_other_two_share_one_commit(owner, setup, blocked,
                                                                                       monkeypatch):
    o = owner
    o.monkeypatch = monkeypatch
    setup(o)
    pub = queue_publication(o)
    mem = queue_memory(o)
    queue_note(o)
    which = blocked(o, pub, mem)
    before, pushed = tip(o), pushes(o)

    report = owner_pass(o)

    landed = commits_since(o, before)
    assert len(landed) == 1, f"the two requests that can land must share one commit, got {len(landed)}"
    assert git_command(o.origin, "rev-parse", "main^") == before
    assert pushes(o) - pushed == 1
    assert note_written(o), "the note must land beside the other landable request"
    if which == "memory":
        assert publication_written(o, pub), "the publication must land although the memory write cannot"
        assert publication(o, pub["id"])["status"] == "published"
        assert contents(o, MEMORY) == "Different main lesson", "the conflicting memory write must not land"
        stuck = memory(o, mem["id"])
        assert stuck in writes.pending(o.store), "the memory write must stay queued"
        reason = stuck.get("error") or stuck.get("reason")
    else:
        assert contents(o, MEMORY) == "Docs lesson from one pass", "the memory write must land without the publication"
        assert memory(o, mem["id"])["status"] == "done"
        assert not publication_written(o, pub), "the stale publication must not land in this pass"
        stuck = publication(o, pub["id"])
        assert stuck in order_publications.pending(o.store), (
            f"the publication must stay queued for the owner, not leave the queue: {stuck['status']}")
        assert not o.case.tracker.list_issues(), "the publication's issues wait with it"
        reason = stuck.get("reason") or stuck.get("error")
    assert reason, (f"the {which} request that cannot land must carry a recorded reason "
                    f"(id {stuck['id']}, status {stuck['status']})")
    assert any(stuck["id"] in action for action in report.actions), (
        f"the pass must report the {which} request it could not land: {report.actions}")
