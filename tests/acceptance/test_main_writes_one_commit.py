"""One merge-owner pass lands every queued main write as one commit with one push.

Exercise the public owner pass (`merge_owner.tick`) with requests queued by the
public producers (`main_write_requests.capture` and a deferred notes flush).
Real Git trees run on a local origin; the existing offline transport imports
commits/pushes instead of running them. Assertions read only origin's main
history, file contents, the request queue and the orchestrator inbox.

Break-it: one commit per request (today) fails the history and push counts; a
skipped request fails the contents/status checks; a doubled push or commit fails
the counts; a conflict that aborts the pass fails the "others still land" checks.
"""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import main_write_requests as writes, merge_owner, orch_inbox, order_flush, order_notes
from foragent.scheduler import TickReport
from foragent.state import StateStore
from test_order import offline_publication  # noqa: F401

NOW = datetime(2026, 11, 6, 20, tzinfo=UTC)
DOCS = ".claude/agent-memory/fa-docs/MEMORY.md"
ORCH = ".claude/agent-memory/fa-orch/MEMORY.new.md"
API = ".claude/agent-memory/fa-api/MEMORY.md"


@pytest.fixture
def owner(offline_publication, tmp_path):
    root, config, tracker, git, calls, origin = offline_publication
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    ctx = SimpleNamespace(repo="owner/repo", repo_root=root, config=replace(config, merge_owner=True),
                          tracker=tracker, git=git, store=store, now=lambda: NOW)
    return SimpleNamespace(ctx=ctx, root=root, git=git, store=store, origin=origin, calls=calls)


def capture(case, name, data, requester):
    path = case.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    row = writes.capture(case.git, case.ctx.config, [name], "Memory", requester, store=case.store)
    assert row is not None
    return row


def queue_notes(case):
    order_notes.append(case.store, kind="decision", boundary="chosen", content="Retained decision")
    result = order_flush.flush(case.root, case.ctx.config, case.ctx.tracker, case.git, case.store,
                               repo=case.ctx.repo, boundary="chosen")
    assert result["status"] == "waiting"


def main_tip(case):
    return git_command(case.origin, "rev-parse", "main")


def main_files(case):
    return git_command(case.origin, "ls-tree", "-r", "--name-only", "main").splitlines()


def contents(case, name):
    """File text on origin's main, or None when main lacks the file."""
    if name not in main_files(case):
        return None
    return git_command(case.origin, "show", f"main:{name}")


def pushes(case):
    return sum(1 for args in case.calls if args[:1] == ["push"])


def owner_pass(case):
    report = TickReport()
    merge_owner.tick(case.ctx, report)
    return report


def commits_since(case, before):
    return git_command(case.origin, "rev-list", "--first-parent", f"{before}..main").splitlines()


def test_pending_requests_land_as_one_commit_with_one_push(owner):
    case = owner
    first = capture(case, DOCS, b"Docs lesson\n", "lane docs")
    second = capture(case, ORCH, b"- [id:x] Orch lesson\n", "lane orch")
    third = capture(case, API, b"Api lesson\n", "role api")
    queue_notes(case)
    assert len(writes.pending(case.store)) == 4
    before, pushed, checkout = main_tip(case), pushes(case), case.git.head_sha()

    owner_pass(case)

    landed = commits_since(case, before)
    assert len(landed) == 1, f"one owner pass must land one commit on main, got {len(landed)}"
    assert git_command(case.origin, "rev-parse", "main^") == before
    assert pushes(case) - pushed == 1, "one owner pass must push main exactly once"
    assert contents(case, DOCS) == "Docs lesson"
    assert contents(case, ORCH) == "- [id:x] Orch lesson"
    assert contents(case, API) == "Api lesson"
    assert contents(case, "docs/decisions.md") == "- Retained decision"
    assert not writes.pending(case.store), "every applied request must reach its done outcome"
    done = {row["id"]: row["status"] for row in writes.requests(case.store)}
    assert all(done[row["id"]] == "done" for row in (first, second, third))
    assert case.git.head_sha() == checkout  # Producer checkout untouched.

    # A second pass has nothing left: no further commit and no further push.
    tip, pushed = main_tip(case), pushes(case)
    owner_pass(case)
    assert main_tip(case) == tip and pushes(case) == pushed


def test_two_file_requests_from_one_pass_share_the_commit(owner):
    """The smallest case: two requests, one commit, both edits present."""
    case = owner
    capture(case, DOCS, b"First\n", "lane docs")
    capture(case, DOCS, b"First\nSecond\n", "lane docs")
    capture(case, ORCH, b"Other lane\n", "lane orch")
    before, pushed = main_tip(case), pushes(case)

    owner_pass(case)

    assert len(commits_since(case, before)) == 1
    assert pushes(case) - pushed == 1
    assert contents(case, DOCS) == "First\nSecond"
    assert contents(case, ORCH) == "Other lane"
    assert not writes.pending(case.store)


@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_conflicting_request_is_reported_and_others_still_land_in_one_commit(owner, git_history, position):
    case = owner
    others = iter([(ORCH, b"Orch lesson\n", "lane orch"), (API, b"Api lesson\n", "role api")])
    rows = []
    for slot in ["first", "middle", "last"]:
        if slot == position:
            rows.append(("conflict", capture(case, DOCS, b"Local docs lesson\n", "lane docs")))
        else:
            name, data, requester = next(others)
            rows.append((name, capture(case, name, data, requester)))
    conflict = next(row for kind, row in rows if kind == "conflict")
    # Main changes the same file differently after the request was captured.
    git_history.update({DOCS: "Different main lesson\n"})
    before, pushed = main_tip(case), pushes(case)

    report = owner_pass(case)

    landed = commits_since(case, before)
    assert len(landed) == 1, f"the non-conflicting requests must land in one commit, got {len(landed)}"
    assert git_command(case.origin, "rev-parse", "main^") == before
    assert pushes(case) - pushed == 1
    assert contents(case, ORCH) == "Orch lesson"
    assert contents(case, API) == "Api lesson"
    assert contents(case, DOCS) == "Different main lesson", "a conflicting request must not land"

    # The conflict is kept and reported as today.
    remaining = writes.pending(case.store)
    assert [row["id"] for row in remaining] == [conflict["id"]]
    assert remaining[0]["status"] == "blocked"
    assert remaining[0]["payload"] == conflict["payload"]
    message, = orch_inbox.requests(case.store)
    assert message["main_write"] == conflict["id"] and "lane docs" in message["text"]
    assert any(conflict["id"] in action and "blocked" in action for action in report.actions)

    # The next pass neither lands it nor reports it again.
    tip, pushed = main_tip(case), pushes(case)
    owner_pass(case)
    assert main_tip(case) == tip and pushes(case) == pushed
    assert len(orch_inbox.requests(case.store)) == 1
    assert writes.pending(case.store)[0]["status"] == "blocked"


def test_only_conflicting_requests_create_no_commit(owner, git_history):
    """A pass whose every request conflicts reports each and pushes nothing."""
    case = owner
    capture(case, DOCS, b"Local docs\n", "lane docs")
    capture(case, API, b"Local api\n", "role api")
    git_history.update({DOCS: "Main docs\n", API: "Main api\n"})
    before, pushed = main_tip(case), pushes(case)

    owner_pass(case)

    assert main_tip(case) == before and pushes(case) == pushed
    assert [row["status"] for row in writes.pending(case.store)] == ["blocked", "blocked"]
    assert sorted(item["requester"] for item in orch_inbox.requests(case.store)) == ["lane docs", "role api"]
    assert DOCS in main_files(case) and contents(case, API) == "Main api"
