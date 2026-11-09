"""Acceptance (batch-recheck-on-overlap-only, statements 1-3).

Jin r1313: while a merge batch is being checked, a main commit that changes
only files none of its members change does not discard the batch; the batch
merges after its final tip check on the main it lands on. A main commit that
changes a file a member changes discards and rebuilds the batch as today.

World: see batch_overlap_world.py (the locked `batched` merge-path world).
Every test here fails on current main, where any non-neutral main commit
retires the train with "target changed".
"""

import pytest

from batch_overlap_world import (
    LEDGER, ledger_main, MEMBERS, NEIGHBOR, SHARED, check_lines, green, groups, is_ancestor, main_text, main_tip,
    members_landed, move_inside_final_checks, move_main, neighbor_main, reasons, record_tip_ci_reads, refs,
    shared_main, support, text_at, tip_ci_turns_red_inside_final_checks, train_tip, waiting_train,
)
from acceptance.test_merge_path_real_settings import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
from test_merge_owner_batch import batched  # noqa: F401


# ---------------------------------------------------------------- statement 1

def test_disjoint_main_commit_during_tip_ci_keeps_the_batch_and_it_merges(batched, monkeypatch):
    case = batched
    pr, tip = waiting_train(case, monkeypatch)
    published = refs(case)
    rounds = len(case.ci.rounds)
    moved = move_main(case, neighbor_main("during tip CI"))

    support.gate(case)  # Owner pass with the train's CI still pending.
    assert reasons(case) == [], (
        f"A main commit changing only {NEIGHBOR} (no member changes it) must not discard the batch; "
        f"retirements: {reasons(case)}")
    assert refs(case) == published and len(groups(case)) == 1, "The same train stays published"
    assert case.tracker.pr_head_sha(pr.number) == tip, "The train tip is not rebuilt"
    assert len(case.ci.rounds) == rounds, "No new train CI starts"

    green(case)
    support.gate(case)
    assert members_landed(case) == set(MEMBERS), "The kept batch merges"
    assert reasons(case) == ["train merged"]
    assert len(groups(case)) == 1, "The batch merged without being rebuilt"
    assert len(case.ci.rounds) == rounds, "The kept batch merges on its original tip CI"
    assert len(case.target_pushes) == 1, "Exactly one landing on main"
    assert case.target_pushes[0][2] == moved, "The batch lands on the moved main"
    new_main = main_tip(case)
    assert is_ancestor(case, moved, new_main) and is_ancestor(case, tip, new_main)
    assert main_text(case, NEIGHBOR) == "OTHER = 'main moved: during tip CI'", "Main's own commit is kept"
    assert main_text(case, SHARED).startswith("VALUE = 5"), "Member 5's change landed"
    for n in MEMBERS:
        assert main_text(case, f"src/member_{n}.py") == f"VALUE = {n}"


def test_disjoint_main_commit_during_final_checks_keeps_the_batch(batched, monkeypatch):
    case = batched
    pr, tip = waiting_train(case, monkeypatch)
    rounds = len(case.ci.rounds)
    green(case)
    moved = move_inside_final_checks(case, monkeypatch, neighbor_main("during final checks"))

    support.gate(case)
    assert moved, "main did not move during the final checks"
    assert reasons(case) == ["train merged"], (
        f"A disjoint main commit during the final checks must not discard the batch; retirements: {reasons(case)}")
    assert members_landed(case) == set(MEMBERS)
    assert len(case.target_pushes) == 1 and case.target_pushes[0][2] == moved[0]
    assert len(groups(case)) == 1 and len(case.ci.rounds) == rounds
    assert is_ancestor(case, moved[0], main_tip(case)) and is_ancestor(case, tip, main_tip(case))


def test_decision_ledger_only_main_commit_keeps_the_batch_and_it_merges(batched, monkeypatch):
    """PM answer: every file counts the same way, including docs/decisions.md."""
    case = batched
    pr, tip = waiting_train(case, monkeypatch)
    rounds = len(case.ci.rounds)
    moved = move_main(case, ledger_main("ledger only"))

    support.gate(case)
    assert reasons(case) == [], (
        f"A main commit changing only {LEDGER} (no member changes it) must not discard the batch; "
        f"retirements: {reasons(case)}")
    assert case.tracker.pr_head_sha(pr.number) == tip and len(groups(case)) == 1
    assert len(case.ci.rounds) == rounds, "No fresh batch CI for a ledger-only move"

    green(case)
    support.gate(case)
    assert reasons(case) == ["train merged"], f"retirements: {reasons(case)}"
    assert members_landed(case) == set(MEMBERS) and len(groups(case)) == 1
    assert len(case.ci.rounds) == rounds
    assert len(case.target_pushes) == 1 and case.target_pushes[0][2] == moved
    assert main_text(case, LEDGER).endswith("- last: main ledger only"), "Main's ledger change is kept"


def test_decision_ledger_only_commit_during_final_checks_keeps_the_batch(batched, monkeypatch):
    case = batched
    waiting_train(case, monkeypatch)
    rounds = len(case.ci.rounds)
    green(case)
    moved = move_inside_final_checks(case, monkeypatch, ledger_main("final checks"))
    support.gate(case)
    assert moved and reasons(case) == ["train merged"], (
        f"A ledger-only commit during the final checks must not discard the batch; retirements: {reasons(case)}")
    assert members_landed(case) == set(MEMBERS) and len(case.ci.rounds) == rounds
    assert len(case.target_pushes) == 1 and case.target_pushes[0][2] == moved[0]


# ---------------------------------------------------------------- statement 2

def test_overlapping_commit_after_a_kept_disjoint_one_discards_and_rebuilds_on_new_main(batched, monkeypatch):
    """The overlap rule still discards: a kept batch is discarded by a later overlapping commit."""
    case = batched
    pr, tip = waiting_train(case, monkeypatch)
    move_main(case, neighbor_main("first, disjoint"))
    support.gate(case)
    assert reasons(case) == [] and len(groups(case)) == 1, (
        f"The disjoint commit must keep the batch; retirements: {reasons(case)}")

    overlapped = move_main(case, shared_main("second, overlapping"))
    support.gate(case)
    assert reasons(case) == ["target changed"], (
        f"A main commit changing {SHARED}, which member 5 changes, discards the batch once; got {reasons(case)}")
    assert not members_landed(case) and not case.target_pushes

    support.gate(case)  # Rebuild on the new main.
    assert len(groups(case)) == 2, "Exactly one rebuilt train (the disjoint commit caused none)"
    rebuilt = train_tip(case, groups(case)[-1])
    assert rebuilt != tip, "The train tip is rebuilt"
    assert is_ancestor(case, overlapped, rebuilt), "The rebuilt train tip descends from the new main"
    assert not is_ancestor(case, tip, rebuilt), "The rebuilt train does not keep the discarded tip"
    shared = text_at(case, rebuilt, SHARED)
    assert shared.startswith("VALUE = 5") and shared.endswith("TAIL = 'main second, overlapping'"), (
        "The rebuilt tip holds member 5's change on top of main's overlapping change")
    for n in MEMBERS:
        assert text_at(case, rebuilt, f"src/member_{n}.py") == f"VALUE = {n}"
    green(case)
    support.gate(case)
    assert members_landed(case) == set(MEMBERS)
    assert reasons(case) == ["target changed", "train merged"]
    assert len(case.target_pushes) == 1
    assert main_text(case, SHARED).startswith("VALUE = 5")
    assert main_text(case, SHARED).endswith("TAIL = 'main second, overlapping'")


# ---------------------------------------------------------------- statement 3

def test_kept_batch_tip_runs_its_final_check_on_the_moved_main(batched, monkeypatch, capsys):
    case = batched
    pr, tip = waiting_train(case, monkeypatch)
    moved = move_main(case, neighbor_main("before final check"))
    support.gate(case)
    assert reasons(case) == [], f"The disjoint commit must keep the batch; retirements: {reasons(case)}"
    capsys.readouterr()

    reads = record_tip_ci_reads(case, monkeypatch, pr)
    green(case)
    support.gate(case)
    lines = check_lines(capsys.readouterr().out, tip)
    assert any(line.startswith("merge owner checks start:") for line in lines), (
        f"The kept batch tip runs the owner's final machine checks after the move; log lines: {lines}")
    assert any(line.startswith("merge owner checks end:") and f"at {tip[:12]} ok " in line for line in lines), (
        f"The final checks of the kept tip finish ok before it merges; log lines: {lines}")
    assert 0 in reads, "The tip CI is read again before the main push (final recheck)"
    assert len(case.target_attempts) == 1 and case.target_attempts[0][2] == moved, (
        "The only main update is leased on the moved main the final check read")
    assert members_landed(case) == set(MEMBERS) and reasons(case) == ["train merged"]
    landed = main_tip(case)
    assert is_ancestor(case, moved, landed) and is_ancestor(case, tip, landed), (
        "The checked tip lands on the moved main without being rebuilt")


def test_kept_batch_does_not_merge_when_its_final_check_on_the_moved_main_fails(batched, monkeypatch):
    case = batched
    pr, tip = waiting_train(case, monkeypatch)
    moved = move_main(case, neighbor_main("final check refuses"))
    support.gate(case)
    assert reasons(case) == [], f"The disjoint commit must keep the batch; retirements: {reasons(case)}"

    green(case)
    flipped = tip_ci_turns_red_inside_final_checks(case, monkeypatch, pr)
    support.gate(case)
    assert flipped, "The kept batch reached its locked final recheck"
    assert not case.target_attempts and not members_landed(case), "A red final tip check blocks the merge"
    assert main_tip(case) == moved
    assert reasons(case) == ["tip CI red"], (
        f"The kept batch is stopped by its final check, not discarded by the move; retirements: {reasons(case)}")


@pytest.mark.parametrize("files", [neighbor_main("twice a"), {"src/other_one.py": "A = 1\n", "src/other_two.py": "B = 2\n"}])
def test_several_disjoint_moves_keep_one_batch(batched, monkeypatch, files):
    case = batched
    pr, tip = waiting_train(case, monkeypatch)
    rounds = len(case.ci.rounds)
    move_main(case, files)
    support.gate(case)
    last = move_main(case, neighbor_main("twice b"))
    support.gate(case)
    assert reasons(case) == [] and len(groups(case)) == 1 and len(case.ci.rounds) == rounds, (
        f"Repeated disjoint commits keep the same batch; retirements: {reasons(case)}")
    green(case)
    support.gate(case)
    assert members_landed(case) == set(MEMBERS) and len(case.target_pushes) == 1
    assert case.target_pushes[0][2] == last
