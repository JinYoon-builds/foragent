"""Guard (batch-recheck-on-overlap-only): regression controls that pass today and must keep passing.

Not in Done when. An overlapping main commit still discards the batch and it is
rebuilt on the new main; the batch tip's final check still runs before merging.
"""

from batch_overlap_world import (
    LEDGER, LEDGER_MEMBER, ledger_main, MEMBERS, SHARED, check_lines, green, groups, is_ancestor, main_text, main_tip,
    members_landed, move_inside_final_checks, move_main, reasons, record_tip_ci_reads, shared_main, support, text_at,
    tip_ci_turns_red_inside_final_checks, train_tip, waiting_train,
)
from acceptance.test_merge_path_real_settings import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
from test_merge_owner_batch import batched  # noqa: F401


def test_overlapping_main_commit_discards_and_rebuilds_on_new_main(batched, monkeypatch):
    case = batched
    pr, tip = waiting_train(case, monkeypatch)
    rounds = len(case.ci.rounds)
    moved = move_main(case, shared_main("overlap"))
    support.gate(case)
    assert reasons(case) == ["target changed"]
    assert not members_landed(case) and not case.target_pushes
    support.gate(case)
    assert len(groups(case)) == 2
    rebuilt = train_tip(case, groups(case)[-1])
    assert rebuilt != tip and is_ancestor(case, moved, rebuilt) and not is_ancestor(case, tip, rebuilt)
    assert text_at(case, rebuilt, SHARED).endswith("TAIL = 'main overlap'")
    assert len(case.ci.rounds) == rounds + 1, "The rebuilt train runs its own tip CI"
    green(case)
    support.gate(case)
    assert members_landed(case) == set(MEMBERS) and len(case.target_pushes) == 1
    assert reasons(case) == ["target changed", "train merged"]
    assert main_text(case, SHARED).endswith("TAIL = 'main overlap'")


def test_ledger_commit_discards_a_batch_whose_member_changes_the_ledger(batched, monkeypatch):
    """The decision ledger counts like any file: when a member changes it, a main change to it overlaps."""
    case = batched
    pr, tip = waiting_train(case, monkeypatch, ledger_member=True)
    moved = move_main(case, ledger_main("overlap"))
    support.gate(case)
    assert reasons(case) == ["target changed"]
    assert not members_landed(case) and not case.target_pushes
    support.gate(case)
    assert len(groups(case)) == 2
    rebuilt = train_tip(case, groups(case)[-1])
    assert rebuilt != tip and is_ancestor(case, moved, rebuilt)
    assert "- first: member 1" in text_at(case, rebuilt, LEDGER)
    green(case)
    support.gate(case)
    assert members_landed(case) == set(MEMBERS) and reasons(case) == ["target changed", "train merged"]
    ledger = main_text(case, LEDGER)
    assert ledger.startswith(LEDGER_MEMBER.split("\n")[0]) and "- first: member 1" in ledger
    assert ledger.endswith("- last: main overlap")


def test_overlapping_main_commit_during_final_checks_discards_the_batch(batched, monkeypatch):
    case = batched
    waiting_train(case, monkeypatch)
    green(case)
    moved = move_inside_final_checks(case, monkeypatch, shared_main("final"))
    support.gate(case)
    assert moved and not case.target_pushes and not members_landed(case)
    assert reasons(case) == ["member head or target changed"]


def test_final_tip_check_runs_before_merging_on_unmoved_main(batched, monkeypatch, capsys):
    case = batched
    pr, tip = waiting_train(case, monkeypatch)
    base = case.history.tip
    capsys.readouterr()
    reads = record_tip_ci_reads(case, monkeypatch, pr)
    green(case)
    support.gate(case)
    lines = check_lines(capsys.readouterr().out, tip)
    assert any(line.startswith("merge owner checks end:") and f"at {tip[:12]} ok " in line for line in lines), lines
    assert 0 in reads, "The tip CI is read again before the main push"
    assert members_landed(case) == set(MEMBERS) and len(case.target_pushes) == 1
    assert case.target_pushes[0][2] == base and is_ancestor(case, tip, main_tip(case))


def test_failed_final_tip_check_blocks_the_merge(batched, monkeypatch):
    case = batched
    pr, tip = waiting_train(case, monkeypatch)
    base = case.history.tip
    green(case)
    flipped = tip_ci_turns_red_inside_final_checks(case, monkeypatch, pr)
    support.gate(case)
    assert flipped and not case.target_attempts and not members_landed(case)
    assert main_tip(case) == base
    assert reasons(case) == ["tip CI red"]
