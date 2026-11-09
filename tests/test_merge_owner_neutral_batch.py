"""A neutral owner write preserves the immutable train and its measured CI."""

from contextlib import contextmanager

from conftest import git_command
from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from test_merge_owner_batch import batched, events, green, queue_workers, refs  # noqa: F401
from foragent import main_write_requests as writes, merge_owner, merge_owner_batch as batch
from foragent.ci_workflow import EXCLUSIONS_TRAILER
from foragent.state import StateStore


def test_neutral_main_write_keeps_open_batch_and_lands_without_new_ci(batched):
    case = batched
    name = ".claude/agent-memory/fa-docs/MEMORY.md"
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    published = refs(case)
    tip = case.ci.rounds[-1].head
    heads = [row["publication"]["head"] for row in merge_owner.admissions(case.store).values()]
    rounds = len(case.ci.rounds)
    before = case.history.tip
    path = case.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Learned while the batch was running\n")
    writes.capture(case.git, case.config, [name], "Lesson", "lane docs", store=case.store)
    support.gate(case)
    moved = case.history.tip
    assert moved != before and not writes.pending(case.store)
    assert refs(case) == published and not batch.retirements(case.ctx)
    assert len(case.ci.rounds) == rounds and not events(case)
    green(case)
    support.gate(case)
    assert len(case.ci.rounds) == rounds and not refs(case)
    assert all(case.git.is_ancestor(head, case.history.tip) for head in heads)
    assert git_command(case.history.origin, "show", f"main:{name}") == "Learned while the batch was running"
    assert case.target_pushes[-1][2] == moved
    assert case.git.at(case.history.origin).latest_trailer(EXCLUSIONS_TRAILER, moved) == (
        case.git._call(["show", "-s", f"--format=%(trailers:key={EXCLUSIONS_TRAILER},valueonly)", tip]).strip())
    assert [row["reason"] for row in batch.retirements(case.ctx)] == ["train merged"]


def test_decision_ledger_move_requires_fresh_batch_ci(batched):
    """Jin r1313 replaced the old rule: a ledger-only move no member touches keeps the batch.

    The name is kept for the runner's baseline; no fresh batch CI is required any more.
    """
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    rounds = len(case.ci.rounds)
    case.history.update({"docs/decisions.md": "# Decisions\n"})
    moved = case.history.tip
    green(case)
    support.gate(case)
    assert [row["reason"] for row in batch.retirements(case.ctx)] == ["train merged"]
    assert len(case.ci.rounds) == rounds, "no fresh batch CI for a ledger-only move"
    assert len(case.target_pushes) == 1 and case.target_pushes[0][2] == moved


def test_neutral_target_move_during_final_checks_keeps_batch(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    lock = StateStore.merge_lock
    moved = []

    @contextmanager
    def move(store):
        with lock(store):
            if not moved:
                case.history.update({".claude/agent-memory/fa-docs/MEMORY.md": "New memory\n"})
                moved.append(case.history.tip)
            yield

    monkeypatch.setattr(StateStore, "merge_lock", move)
    rounds = len(case.ci.rounds)
    support.gate(case)
    assert len(case.target_pushes) == 1 and case.target_pushes[0][2] == moved[0]
    assert len(case.ci.rounds) == rounds + 1  # Only the original train tip ran CI.
    assert git_command(case.history.origin, "show", "main:.claude/agent-memory/fa-docs/MEMORY.md") == "New memory"
    assert [row["reason"] for row in batch.retirements(case.ctx)] == ["train merged"]


def test_kept_batch_checks_the_commit_that_lands_on_the_moved_main(batched):
    """Jin r1313: a kept batch is checked as its merge with the moved main, not as the bare tip.

    The locked test passes on the tip and on the moved main separately and fails only on
    their combination, so a check of the bare tip (or a skip from the tip's CI) lets it land.
    """
    from dataclasses import replace
    from helpers.merge_batch import update_base

    case = batched
    own = "tests/acceptance/test_own_5.py"
    update_base(case, {"docs/decisions.md": "# Decisions\n", own: (
        "from pathlib import Path\ndef test_own():\n"
        "    ledger = Path('docs/decisions.md').read_text()\n"
        "    assert not (Path('src/member_5.py').exists() and 'moved' in ledger)\n")})
    approval = case.store.load_approval(5)
    case.store.save_approval(**replace(approval, acceptance_blobs=case.git.blob_shas(case.base, [own])).__dict__)
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    rounds = len(case.ci.rounds)
    case.history.update({"docs/decisions.md": "# Decisions\n- moved\n"})
    moved = case.history.tip
    green(case)
    support.gate(case)
    assert len(case.ci.rounds) == rounds, "the ledger-only move kept the batch"
    assert not case.target_pushes and case.history.tip == moved, "the failing combination must not land"
    retired = [row["reason"] for row in batch.retirements(case.ctx)]
    assert len(retired) == 1 and retired[0].startswith("member #5:"), retired
