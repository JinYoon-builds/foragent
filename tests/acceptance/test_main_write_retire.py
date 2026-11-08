"""Acceptance (main-write-retire): the orchestrator retires a blocked main-write request.

q86 (2026-11-08): request 106 (a lock removal) was blocked and held every later
main write; no fa command could retire it. After this issue

    fa orch main-write retire ID --why TEXT

marks one blocked request retired with the reason, and the merge owner's next
pass lands the next pending request. A pending or done request is refused with
a non-zero exit and one plain line, and nothing changes.

Public entry points: the producer `main_write_requests.capture`, the owner pass
`merge_owner.tick`, the CLI `foragent.cli.main` and `status.collect` (what
`fa gate status` shows). Real Git on a local bare origin through the shared
offline transport (fixtures of tests/acceptance/test_one_commit_per_pass.py,
locked, read-only). GitHub is the fake of tests/test_order_publications.py.
No network, model or signal.

Break-it: a retire that does not change the blocked row (or keeps it blocked)
leaves the next request unlanded; a retire that applies the blocked content or
writes to main fails the content and tip checks; a doubled landing fails the
commit and push counts; a refusal that still edits the queue fails the byte
checks; accepting a pending or done request fails the exit-code check.
"""

from pathlib import Path

import pytest

from conftest import git_command
from foragent import main_write_requests as writes, status
from test_one_commit_per_pass import (  # noqa: F401  (fixtures)
    MEMORY, case, commits_since, contents, owner, owner_pass, pushes, queue_memory, tip)
from test_order import offline_publication  # noqa: F401  (fixture)

LATER = ".claude/agent-memory/fa-orch/MEMORY.md"
WHY = "Superseded locked tests; the lock removal must not land (q86)"


def bind_cli(o, monkeypatch):
    """`fa ...` on the owner's checkout, config, fake GitHub and the store the owner reads."""
    from foragent import cli
    from foragent.state import StateStore

    monkeypatch.setattr(cli, "load_config", lambda root, *a, **kw: o.ctx.config)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: o.ctx.tracker)
    monkeypatch.setattr(cli, "GitRepo", lambda root: o.git)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: o.store))
    monkeypatch.setattr(cli, "kick_tick", lambda *a, **kw: None, raising=False)
    monkeypatch.setenv("FORAGENT_VIEW", "off")

    def fa(capsys, *argv):
        capsys.readouterr()
        try:
            code = cli.main([*argv, "--repo-root", str(o.root)])
        except SystemExit as exc:
            code = exc.code
        captured = capsys.readouterr()
        return (0 if code is None else code), captured.out + captured.err

    return fa


def row(o, identity):
    return next(r for r in writes.requests(o.store) if r["id"] == identity)


def queue_later(o, data=b"Orch lesson behind the blocked write\n"):
    path = o.root / LATER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    later = writes.capture(o.git, o.ctx.config, [LATER], "Orch memory", "lane orch", store=o.store)
    assert later is not None and later["status"] == "pending"
    return later


def blocked_request(o):
    """A memory write that main changed differently after it was captured: the owner blocks it."""
    stuck = queue_memory(o)
    o.history.update({MEMORY: "Different main lesson\n"})
    owner_pass(o)
    stuck = row(o, stuck["id"])
    assert stuck["status"] == "blocked", f"setup: the request must be blocked: {stuck}"
    return stuck


def blocked_queue(o):
    """A blocked request holds a later pending one in FIFO order, as request 106 held 176."""
    stuck = blocked_request(o)
    later = queue_later(o)
    before, pushed = tip(o), pushes(o)
    owner_pass(o)
    assert tip(o) == before and pushes(o) == pushed, "setup: the blocked request must hold the queue"
    assert contents(o, LATER) is None and row(o, later["id"])["status"] == "pending"
    return stuck, later


def state_bytes(o):
    """Every file of the main-write queue and the rest of the state, byte for byte."""
    return {path.relative_to(o.store.root).as_posix(): path.read_bytes()
            for path in sorted(Path(o.store.root).rglob("*.json")) if path.is_file()}


# Statement 1 ----------------------------------------------------------------------------

def test_retiring_the_blocked_request_lets_the_next_pass_land_the_next_pending_one(owner, monkeypatch, capsys):
    o = owner
    fa = bind_cli(o, monkeypatch)
    stuck, later = blocked_queue(o)
    before, pushed = tip(o), pushes(o)

    code, out = fa(capsys, "orch", "main-write", "retire", stuck["id"], "--why", WHY)

    assert code == 0, f"retiring a blocked main-write request must succeed: {out}"
    assert tip(o) == before and pushes(o) == pushed, "retiring must write nothing to main"
    retired = row(o, stuck["id"])
    assert retired["status"] == "retired", f"the blocked request must be retired: {retired}"
    assert WHY in str(retired.get("reason")), f"the retired request must keep the reason: {retired}"
    assert stuck["id"] not in {r["id"] for r in writes.pending(o.store)}
    assert row(o, later["id"])["status"] == "pending", "retiring one request leaves the next one queued"

    report = owner_pass(o)

    landed = commits_since(o, before)
    assert len(landed) == 1, f"the next pass must land the next pending request once, got {len(landed)} commits"
    assert pushes(o) - pushed == 1, f"the next pass must push main once, got {pushes(o) - pushed}"
    assert contents(o, LATER) == "Orch lesson behind the blocked write", "the next request must land on main"
    assert row(o, later["id"])["status"] == "done", row(o, later["id"])
    assert contents(o, MEMORY) == "Different main lesson", "the retired request's content must never land"
    assert row(o, stuck["id"])["status"] == "retired", "the pass must not revive the retired request"
    assert any(later["id"] in action for action in report.actions), report.actions

    # Nothing is left: a later pass moves main no further.
    after, pushed = tip(o), pushes(o)
    owner_pass(o)
    assert tip(o) == after and pushes(o) == pushed


def test_gate_status_shows_the_retired_request_with_its_reason(owner, monkeypatch, capsys):
    o = owner
    fa = bind_cli(o, monkeypatch)
    stuck, _later = blocked_queue(o)

    code, out = fa(capsys, "orch", "main-write", "retire", stuck["id"], "--why", WHY)
    assert code == 0, out

    shown = status.collect(o.store, o.ctx.config, o.ctx.now())["main_writes"]
    retired = [r for r in shown if r.get("id") == stuck["id"]]
    assert retired, f"fa gate status must show the retired request: {shown}"
    assert retired[0].get("status") == "retired", retired
    assert WHY in " ".join(str(v) for v in retired[0].values()), f"status must show the reason: {retired}"


# Statement 2 ----------------------------------------------------------------------------

def pending_request(o):
    return queue_later(o)


def done_request(o):
    later = queue_later(o)
    owner_pass(o)
    later = row(o, later["id"])
    assert later["status"] == "done", f"setup: the request must be done: {later}"
    return later


@pytest.mark.parametrize("make", [pending_request, done_request], ids=["pending", "done"])
def test_retiring_a_pending_or_done_request_is_refused_and_changes_nothing(owner, monkeypatch, capsys, make):
    o = owner
    fa = bind_cli(o, monkeypatch)
    target = make(o)
    saved, before, pushed = state_bytes(o), tip(o), pushes(o)

    code, out = fa(capsys, "orch", "main-write", "retire", target["id"], "--why", WHY)

    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, f"the refusal must be one plain line, got: {out!r}"
    assert target["id"] in lines[0] or target["status"] in lines[0] or "blocked" in lines[0], (
        f"the refusal must name the request or why it cannot be retired: {lines[0]!r}")
    assert code != 0, f"retiring a {target['status']} request must exit non-zero: {out}"
    assert state_bytes(o) == saved, "a refused retire must change no state"
    assert tip(o) == before and pushes(o) == pushed, "a refused retire must write nothing to main"
    assert row(o, target["id"])["status"] == target["status"]
