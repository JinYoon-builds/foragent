"""Acceptance: the docs owner lands its own doc edits through the main-write queue
(order 2026-11-09 invoice step 1, issue docs-owner-lands-own-edits; Jin r1322, architect r1321).

Public entry points only:
- the docs owner's session is `orch_architect.run_session(..., role="docs")` (what
  `fa gate run-architect --role docs` runs) with a fake executor that edits files;
- the ordinary role-commit tick step `orch_role_memory.publish` (the step that today sends only
  role memory folders to main) runs after the session, so a submission made either at the end of
  the session or by that tick step is seen;
- the merge owner's pass is `merge_owner.tick`; the main-write queue is read with
  `main_write_requests.requests`;
- requests are filed and decided with `fa orch request` and `fa orch role-decision`; the gate's
  per-tick shelf step is `shelf.ask` and the place counts come from `shelf.summary`;
- the skills are read as shipped text.

Real Git trees run on a local bare origin; the shared offline transport (tests/test_order.py)
imports commits and pushes. A merge train is a ref on origin whose CI passes on a chosen owner
pass (the Train helper of tests/acceptance/queue_only_main_world.py). No network, model or signal.

Break-it: a docs session whose docs/ edit is never queued fails the queue checks; landing the
edit twice (or queueing it again after it landed) fails the commit and queue counts; landing
the wrong bytes fails the content check; a train paused for the docs write fails the train
check; an accepted request that leaves the place at once (today) or on any other receipt fails
the alarm and count checks; one that never leaves after its receipt lands fails the final count.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import executors, main_write_requests as writes, merge_owner, orch_role_memory, shelf
from foragent.executors import ExecResult
from foragent.orch_architect import run_session
from foragent.scheduler import TickReport
from foragent.state import StateStore
from queue_only_main_world import Train
from test_order import offline_publication  # noqa: F401
from test_orch_review_handoff import bind_cli
from test_skills_autosync import cli_exit

ROOT = Path(__file__).resolve().parents[2]
GUIDE = "docs/guide.md"
OLD_GUIDE = "# Guide\n\nThe gate starts one run per lane.\n"
NEW_GUIDE = "# Guide\n\nThe gate starts one run per lane.\nThe merge owner lands queued main writes between batches.\n"
OTHER = "notes/other-session.md"
FOLDERS = {"docs": ".claude/agent-memory/fa-docs", "architect": ".claude/agent-memory/fa-architect"}


# ---------------------------------------------------------------- world


@pytest.fixture
def docs_world(offline_publication, git_history, tmp_path, monkeypatch, capsys):
    """A merge-owner project whose main tracks docs/guide.md; the checkout matches main."""
    root, config, tracker, git, calls, origin = offline_publication
    git_history.update({GUIDE: OLD_GUIDE, f"{FOLDERS['docs']}/MEMORY.md": "# Memory\n",
                        f"{FOLDERS['architect']}/MEMORY.md": "# Memory\n"}, message="Docs on main")
    git_command(root, "fetch", "origin")
    git_command(root, "merge", "--ff-only", "origin/main")
    roles = {name: {"agent": f"fa-{name}", "skills": [], "memory": folder + "/", "signals": []}
             for name, folder in FOLDERS.items()}
    clock = [datetime.now(UTC).replace(microsecond=0)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    (store.root / "logs").mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(repo="owner/repo", repo_root=root,
                          config=replace(config, repo="owner/repo", merge_owner=True, roles=roles, language="en"),
                          tracker=tracker, git=git, store=store, now=lambda: clock[0])
    assert git_command(root, "symbolic-ref", "--short", "HEAD") == ctx.config.base_branch
    case = SimpleNamespace(root=root, ctx=ctx, tracker=tracker, git=git, store=store, origin=origin,
                           calls=calls, clock=clock, capsys=capsys, monkeypatch=monkeypatch)
    case.main = bind_cli(case, monkeypatch)
    return case


def later(case, **delta):
    case.clock[0] += timedelta(**delta)


def docs_session(case, content=NEW_GUIDE):
    """One docs owner session that edits docs/guide.md (and records a lesson), then the role-commit step."""

    def execute(**kwargs):
        worktree = Path(kwargs["worktree"])
        (worktree / GUIDE).write_text(content, encoding="utf-8")
        (worktree / FOLDERS["docs"] / "MEMORY.md").write_text("# Memory\n- Guide wording fixed.\n")
        return ExecResult(0, False, "Guide updated.", kwargs["log_path"])

    case.monkeypatch.setattr(executors, "executor_for", lambda _name: SimpleNamespace(run=execute))
    started = case.clock[0].isoformat()
    assert run_session(case.root, case.store, case.ctx.config, "Docs drift on the guide", started,
                       lambda: case.clock[0], role="docs") == 0
    orch_role_memory.publish(case.ctx, TickReport())


def carrying(case, name, *, statuses=None):
    """Main-write requests whose payload changes ``name``."""
    rows = [row for row in writes.requests(case.store)
            if row["payload"].get("kind") == "files" and name in row["payload"].get("files", {})]
    return [row for row in rows if statuses is None or row["status"] in statuses]


def queued_bytes(row, name):
    import base64

    return base64.b64decode(row["payload"]["files"][name]["after"]["bytes"]).decode()


def owner_pass(case):
    report = TickReport()
    merge_owner.tick(case.ctx, report)
    return report


def main_tip(case):
    return git_command(case.origin, "rev-parse", "main")


def on_main(case, name):
    names = git_command(case.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    return git_command(case.origin, "show", f"main:{name}") + "\n" if name in names else None


def commits_touching(case, before, name):
    return git_command(case.origin, "rev-list", f"{before}..main", "--", name).splitlines()


def receipt_on_main(case, identity):
    log = git_command(case.origin, "log", "--format=%B", "main")
    return f"{writes.TRAILER}: {identity}" in log


def the_docs_request(case):
    """The one main-write request that carries the docs owner's docs/guide.md edit."""
    rows = carrying(case, GUIDE)
    assert rows, ("A docs owner session that edits docs/guide.md must submit the edit as a main-write "
                  "request; the queue holds no request that changes docs/guide.md")
    assert len(rows) == 1, f"The docs edit must be queued once, got {len(rows)} requests"
    (row,) = rows
    assert queued_bytes(row, GUIDE) == NEW_GUIDE, "The request must carry the session's edited bytes"
    assert "docs" in row["requester"], f"The request must name the docs owner, got {row['requester']!r}"
    return row


def cli(case, *argv):
    case.capsys.readouterr()
    code = cli_exit(case.main, [*argv, "--repo-root", str(case.root)])
    output = case.capsys.readouterr()
    assert code == 0, (argv, output.out, output.err)
    return output.out


def inbox(case, *, pending=False):
    return json.loads(cli(case, "orch", "inbox", "--json", *(["--pending"] if pending else [])))


def file_request(case, source, text):
    before = {row["id"] for row in inbox(case)}
    cli(case, "orch", "request", "--from", source, "--text", text)
    (row,) = [row for row in inbox(case) if row["id"] not in before]
    return row


def requests_alarms(case):
    return [row for row in inbox(case, pending=True) if row.get("from") == "gate"
            and row.get("kind") == "shelf-alarm" and row.get("place") == "requests"]


def requests_count(case):
    return shelf.summary(case.store)["requests"]["count"]


# ---------------------------------------------------------------- statement 1


def test_docs_session_edit_is_queued_and_the_owner_lands_it_once_on_main(docs_world):
    case = docs_world
    (case.root / OTHER).parent.mkdir(parents=True, exist_ok=True)
    (case.root / OTHER).write_text("Another session's unrelated work.\n")
    before = main_tip(case)

    docs_session(case)

    row = the_docs_request(case)
    assert row["status"] == "pending"
    assert on_main(case, GUIDE) == OLD_GUIDE, "The session itself must not write main; the owner lands it"

    owner_pass(case)

    assert on_main(case, GUIDE) == NEW_GUIDE, "The merge owner must land the docs owner's edit on main"
    assert len(commits_touching(case, before, GUIDE)) == 1, "The docs edit must land in exactly one commit"
    assert receipt_on_main(case, row["id"]), "The landing commit must carry the request's main-write receipt"
    assert carrying(case, GUIDE, statuses={"done"}) and not carrying(case, GUIDE, statuses={"pending", "held",
                                                                                           "blocked"})
    assert on_main(case, OTHER) is None, "Only the docs owner's docs/ edit lands, not other checkout files"

    # An idle cycle after landing queues nothing again and lands nothing again.
    tip = main_tip(case)
    orch_role_memory.publish(case.ctx, TickReport())
    owner_pass(case)
    assert len(carrying(case, GUIDE)) == 1, "A landed docs edit must not be queued a second time"
    assert main_tip(case) == tip, "A second owner pass must not land the docs edit again"


def test_docs_edit_lands_between_batches_while_the_train_keeps_running(docs_world, monkeypatch):
    case = docs_world
    box = SimpleNamespace(origin=case.origin, main=lambda: main_tip(case))
    train = Train(box, monkeypatch, ci_passes_at_pass=2)
    before = main_tip(case)

    docs_session(case)
    row = the_docs_request(case)

    for _ in range(4):
        owner_pass(case)

    assert train.landed_commit is not None and not train.running, (
        "The train must keep running and land on its own schedule while the docs write waits its turn")
    assert train.steps >= 2
    assert on_main(case, GUIDE) == NEW_GUIDE, "The docs owner's edit must land on main between batches"
    assert on_main(case, Train.FILE) is not None
    landing = commits_touching(case, before, GUIDE)
    assert len(landing) == 1, "The docs edit must land in exactly one commit"
    assert landing[0] != train.landed_commit, "The docs edit lands as its own main write, not inside the train"
    assert receipt_on_main(case, row["id"])
    assert [r["status"] for r in carrying(case, GUIDE)] == ["done"]


# ---------------------------------------------------------------- statement 2


def test_accepted_docs_request_stays_on_the_shelf_until_its_main_write_receipt_is_on_main(docs_world):
    case = docs_world
    # An earlier, unrelated main write already landed: its receipt is on main.
    (case.root / FOLDERS["architect"] / "MEMORY.md").write_text("# Memory\n- Architect lesson.\n")
    earlier = writes.capture(case.git, case.ctx.config, [FOLDERS["architect"]], "Memory", "roles architect",
                             store=case.store)
    assert earlier is not None
    owner_pass(case)
    assert receipt_on_main(case, earlier["id"])

    proposal = file_request(case, "docs", "Proposal: the guide must say the merge owner lands main writes.")
    docs_session(case)
    landing = the_docs_request(case)
    assert not receipt_on_main(case, landing["id"])
    cli(case, "orch", "role-decision", proposal["id"], "accepted",
        "--answer", f"Accepted; the docs owner lands it as main-write {landing['id']}.")

    shelf.ask(case.store)
    assert requests_count(case) == 1, (
        "An accepted docs request whose main-write receipt is not on main must stay in the 'requests' place")

    later(case, hours=25)
    shelf.ask(case.store)
    alarms = requests_alarms(case)
    assert len(alarms) == 1, "The waiting accepted request must raise one 'requests' shelf alarm after 24 hours"
    assert proposal["id"] in alarms[0].get("items", []), "The alarm must name the accepted docs request"
    shelf.ask(case.store)
    assert len(requests_alarms(case)) == 1, "Another tick must not raise a second alarm for the same episode"

    owner_pass(case)
    assert receipt_on_main(case, landing["id"]) and on_main(case, GUIDE) == NEW_GUIDE

    shelf.ask(case.store)
    assert requests_count(case) == 0, (
        "Once the main-write receipt is on main the accepted docs request must leave the 'requests' place")
    assert not requests_alarms(case), "The 'requests' alarm must clear when the request leaves"
    shelf.ask(case.store)
    assert requests_count(case) == 0


# ---------------------------------------------------------------- statement 3


SKILLS = ("skills/fa-docs/SKILL.md", "skills/fa-architect/SKILL.md")
WAIT = re.compile(r"\bwait(?:s|ing)?\b", re.IGNORECASE)
KEEP = re.compile(r"memory|restart|survives|give each wait", re.IGNORECASE)
PLACE = re.compile(r"[`'‘\"]?requests[`'’\"]?\s+(?:shelf\s+)?place|place\s+[`'‘\"]?requests\b", re.IGNORECASE)


def sentences(path):
    text = " ".join((ROOT / path).read_text(encoding="utf-8").split())
    return re.split(r"(?<=[.!?])\s+", text)


@pytest.mark.parametrize("path", SKILLS)
def test_role_skill_no_longer_keeps_wait_lines_and_names_the_requests_place(path):
    keeping = [s for s in sentences(path) if WAIT.search(s) and KEEP.search(s)]
    assert not keeping, f"{path} must not tell the role to keep waits in its memory or tracking: {keeping}"
    assert "Wait:" not in (ROOT / path).read_text(encoding="utf-8")
    text = " ".join((ROOT / path).read_text(encoding="utf-8").split())
    assert PLACE.search(text), f"{path} must say the shelf place 'requests' holds the handoff"
