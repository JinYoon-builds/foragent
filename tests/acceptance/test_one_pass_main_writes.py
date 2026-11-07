"""More acceptance checks for one-commit-per-pass (the locked file stays unchanged).

One merge-owner pass that applies several non-PR main-write requests (an order
publication, a decision note and a role memory write) produces exactly one
commit and one push on main; a request that cannot land stays queued with a
recorded reason while the others share one commit, and a later pass does not write the landed ones again.

The idle-pass check (no request -> no commit, no push) already passes on main, so it
lives in test_one_pass_idle_regression.py, outside Done when.

The fixtures and queue helpers come from tests/acceptance/test_one_commit_per_pass.py
(locked, read-only). Entry points: `order.publish` + `order_publications.check_pending`,
`main_write_requests.capture`, `order_notes.append` + `order_flush.flush`, and the
owner pass `merge_owner.tick`. Real Git on a local bare origin; no network,
model or signal.

Break-it: a separate publication commit (today) fails the one-commit and
one-push counts and the "one commit changes every path" check; a skipped
request fails the path and content checks; a doubled commit or push fails the
counts and the later-pass check; a blocked request that is dropped or has no
reason fails the queue and reason checks.
"""

from conftest import git_command
from foragent import main_write_requests as writes, order_prepare_check, order_publications
from foragent.scheduler import TickReport
from test_one_commit_per_pass import (  # noqa: F401  (fixtures)
    DECISIONS, MEMORY, case, commits_since, contents, memory, note_written, offline_publication, owner,
    owner_pass, publication, publication_written, pushes, queue_memory, queue_note, queue_publication, tip)
from test_one_commit_per_pass import SUPPORT, import_support, review_plan  # noqa: F401


def block_publication_still_red(o, pub, monkeypatch):
    """Main moves in a module the publication's test imports, and the test stays red there.

    The move touches the test's imports, so its red certification must be re-run before it
    lands; the measured runner is unavailable in this pass, so it cannot land now. VALUE = 3
    keeps the locked test (`assert VALUE == 2`) red, so a correct owner has no reason to send
    the request back to preparation instead of keeping it queued.
    """
    assert "one_pass_support" in pub["files"][next(iter(pub["files"]))]
    o.history.update({SUPPORT: "VALUE = 3\n"})

    def unavailable(*args, **kwargs):
        raise OSError("test runner unavailable in this pass")

    monkeypatch.setattr(order_prepare_check, "run", unavailable)


def changed_paths(o, sha):
    out = git_command(o.origin, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--first-parent", sha)
    return set(out.splitlines())


def test_one_commit_carries_every_path_of_publication_memory_and_note(owner):
    o = owner
    pub = queue_publication(o)
    mem = queue_memory(o)
    queue_note(o)
    before, pushed = tip(o), pushes(o)

    owner_pass(o)

    landed = commits_since(o, before)
    assert len(landed) == 1, f"one pass must move main once for its non-PR requests, got {len(landed)} commits"
    assert pushes(o) - pushed == 1, f"one pass must push main once, got {pushes(o) - pushed}"
    paths = changed_paths(o, landed[0])
    assert set(pub["files"]) <= paths, f"the commit must carry the publication's files: {sorted(paths)}"
    assert MEMORY in paths, f"the commit must carry the memory write: {sorted(paths)}"
    assert DECISIONS in paths, f"the commit must carry the decision note: {sorted(paths)}"
    assert publication(o, pub["id"])["status"] == "published"
    assert memory(o, mem["id"])["status"] == "done"


def test_publication_and_memory_without_note_share_one_commit(owner):
    o = owner
    pub = queue_publication(o)
    mem = queue_memory(o)
    before, pushed = tip(o), pushes(o)

    owner_pass(o)

    landed = commits_since(o, before)
    assert len(landed) == 1, f"a publication and a memory write must share one commit, got {len(landed)}"
    assert pushes(o) - pushed == 1
    assert publication_written(o, pub) and contents(o, MEMORY) == "Docs lesson from one pass"
    assert memory(o, mem["id"])["status"] == "done"


def test_publication_and_note_share_one_commit(owner):
    o = owner
    pub = queue_publication(o)
    queue_note(o)
    before, pushed = tip(o), pushes(o)

    owner_pass(o)

    landed = commits_since(o, before)
    assert len(landed) == 1, f"a publication and a decision note must share one commit, got {len(landed)}"
    assert pushes(o) - pushed == 1
    assert publication_written(o, pub) and note_written(o)


def test_blocked_memory_leaves_publication_and_note_in_one_commit(owner, git_history):
    o = owner
    pub = queue_publication(o)
    mem = queue_memory(o)
    queue_note(o)
    git_history.update({MEMORY: "Different main lesson\n"})
    order_publications.check_pending(o.ctx, TickReport())
    before, pushed = tip(o), pushes(o)

    owner_pass(o)

    landed = commits_since(o, before)
    assert len(landed) == 1, f"the publication and the note must share one commit, got {len(landed)}"
    assert pushes(o) - pushed == 1
    paths = changed_paths(o, landed[0])
    assert set(pub["files"]) <= paths and DECISIONS in paths, sorted(paths)
    assert MEMORY not in paths, "the conflicting memory write must not be in the commit"
    stuck = memory(o, mem["id"])
    assert stuck in writes.pending(o.store), "the memory write must stay queued"
    assert stuck.get("error") or stuck.get("reason"), f"the stuck memory write needs a recorded reason: {stuck}"


def test_blocked_publication_stays_queued_with_reason_and_others_share_one_commit(owner, monkeypatch):
    o = owner
    import_support(o)
    pub = queue_publication(o)
    mem = queue_memory(o)
    queue_note(o)
    block_publication_still_red(o, pub, monkeypatch)
    before, pushed = tip(o), pushes(o)

    report = owner_pass(o)

    landed = commits_since(o, before)
    assert len(landed) == 1, f"the memory write and the note must share one commit, got {len(landed)}"
    assert pushes(o) - pushed == 1
    paths = changed_paths(o, landed[0])
    assert MEMORY in paths and DECISIONS in paths, sorted(paths)
    assert not set(pub["files"]) & paths, "the blocked publication's files must not be in the commit"
    stuck = publication(o, pub["id"])
    assert stuck in order_publications.pending(o.store), f"the publication must stay queued: {stuck['status']}"
    assert stuck.get("reason") or stuck.get("error"), f"the stuck publication needs a recorded reason: {stuck}"
    assert any(pub["id"] in action for action in report.actions), report.actions
    assert memory(o, mem["id"])["status"] == "done"

    # The blocked request does not make later passes move main again for the landed ones.
    after, pushed = tip(o), pushes(o)
    owner_pass(o)
    assert tip(o) == after and pushes(o) == pushed, "landed requests must not be written a second time"
