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
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    rounds = len(case.ci.rounds)
    case.history.update({"docs/decisions.md": "# Decisions\n"})
    moved = case.history.tip
    green(case)
    case.pending_batch = True
    support.gate(case)
    assert case.history.tip == moved and not case.target_pushes
    assert [row["reason"] for row in batch.retirements(case.ctx)] == ["target changed"]
    support.gate(case)
    assert len(case.ci.rounds) == rounds + 1
    assert refs(case) and not case.target_pushes
    green(case)
    support.gate(case)
    assert case.target_pushes[-1][2] == moved


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
