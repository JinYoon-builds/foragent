"""Acceptance (checked-pr-lands-first, PM r1438): the batch form of statements 1 and 2.

A merge batch whose tip checks on the current main ended ok lands first in the
owner pass; queued order publications and main write requests apply after it, on
the main it produced, and never make the owner re-check or rebuild the batch.

World: the locked `batched` merge-path world with batch_overlap_world.waiting_train
(members 5 and 1), plus checked_first_world.install for the queued writes (real
memory write through the owner's drain; order publication stand-in at
`order_publications.apply_next`). Every test here fails on current main, where
the queued writes move main before the batch lands.
"""

import pytest

from batch_overlap_world import MEMBERS, green, groups, members_landed, reasons, waiting_train
from checked_first_world import (
    MEMORY, MEMORY_TEXT, PUBLICATION, PUBLICATION_TEXT, commits_touching, install, is_ancestor, main_text, main_tip,
    memory_status, queue_memory, queue_publication, support,
)
from foragent import main_write_requests as writes
from acceptance.test_merge_path_real_settings import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
from test_merge_owner_batch import batched  # noqa: F401

QUEUED = ["memory", "publication", "both"]


def train_rounds(case):
    """Train CI rounds (pushes to main by the queued writes start none)."""
    return [row for row in case.ci.rounds if row.branch != "main"]


def checked_train(case, monkeypatch, capsys):
    """The train's tip CI on the current main has ended ok; it is ready to land."""
    install(case, monkeypatch)
    pr, tip = waiting_train(case, monkeypatch)
    green(case)
    checked = main_tip(case)
    capsys.readouterr()
    return pr, tip, checked


def batch_landed_first(case, tip, checked):
    """The first main update holds the checked train tip and is leased on the checked main."""
    if not case.target_pushes:
        return False
    head, _destination, expected = case.target_pushes[0]
    return expected == checked and is_ancestor(case, tip, head)


def queue(case, which):
    row = None
    if which in {"publication", "both"}:
        queue_publication(case)
    if which in {"memory", "both"}:
        row = queue_memory(case)
    return row


@pytest.mark.parametrize("which", QUEUED)
def test_checked_batch_lands_on_its_checked_main_before_the_queued_writes(batched, monkeypatch, capsys, which):
    case = batched
    pr, tip, checked = checked_train(case, monkeypatch, capsys)
    rounds = len(train_rounds(case))
    queue(case, which)

    support.gate(case)
    assert members_landed(case) == set(MEMBERS), (
        f"The batch's checks on the current main ended ok; it lands in this pass before the queued {which} write")
    assert reasons(case) == ["train merged"], f"The batch is not discarded or rebuilt; retirements: {reasons(case)}"
    assert len(groups(case)) == 1 and len(train_rounds(case)) == rounds, "No rebuilt train and no new train CI"
    assert case.tracker.pr_head_sha(pr.number) == tip, "The checked tip is the one that lands"
    assert batch_landed_first(case, tip, checked), (
        f"The first main update is the batch, leased on the main its checks ran on ({checked[:12]}); "
        f"a queued {which} write landed before it; main updates: {case.target_pushes}")


@pytest.mark.parametrize("which", QUEUED)
def test_queued_writes_apply_after_the_batch_on_the_main_it_produced(batched, monkeypatch, capsys, which):
    case = batched
    pr, tip, checked = checked_train(case, monkeypatch, capsys)
    row = queue(case, which)

    support.gate(case)
    assert members_landed(case) == set(MEMBERS) and batch_landed_first(case, tip, checked), (
        f"The batch lands first, on the main its checks ran on; main updates: {case.target_pushes}")
    produced = case.target_pushes[0][0]

    def left():
        return ([] if row is None or memory_status(case, row) == "done" else ["memory"]) + (
            ["publication"] if which != "memory" and not case.publication["applied_on"] else [])

    if left():
        support.gate(case)  # The same or the next owner pass.
    assert left() == [], f"The queued {which} write applies in the same or the next pass; still queued: {left()}"
    if row is not None:
        landed = commits_touching(case, MEMORY, checked)
        assert len(landed) == 1 and landed[0] != produced and is_ancestor(case, produced, landed[0]), (
            "The memory write lands once, after the batch, on the main the batch produced")
        assert main_text(case, MEMORY) == MEMORY_TEXT and row not in writes.pending(case.store)
    if which != "memory":
        assert len(case.publication["applied_on"]) == 1
        assert is_ancestor(case, produced, case.publication["applied_on"][0]), (
            "The publication applies on the main the batch produced")
        assert len(commits_touching(case, PUBLICATION, checked)) == 1
        assert main_text(case, PUBLICATION).startswith(PUBLICATION_TEXT)
    assert is_ancestor(case, produced, main_tip(case)) and reasons(case) == ["train merged"]
    for n in MEMBERS:
        assert main_text(case, f"src/member_{n}.py") == f"VALUE = {n}"
