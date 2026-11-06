"""The reviewing worker's round, from the records the runner writes for each round (#2323).

The runner saves review-<round>.txt when the reviewer (or the panel's findings merge)
answers, scores the findings, and journals the round only when it is decided."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from foragent import watch_data

START = datetime(2026, 11, 6, 12, tzinfo=UTC)


def _store(tmp_path, issue, names, at=None):
    folder = tmp_path / "runs" / f"issue-{issue}"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = folder / name
        path.write_text("VERDICT: FAIL\n", encoding="utf-8")
        if at is not None:
            os.utime(path, (at.timestamp(), at.timestamp()))
    return SimpleNamespace(root=tmp_path)


def _journal(issue, *rounds):
    return [{"kind": "review", "issue": issue, "round": number, "verdict": "FAIL"} for number in rounds]


def _run(number, phase="reviewing"):
    return {"number": number, "phase": phase, "started_at": START.isoformat()}


def test_every_saved_round_decided_means_the_next_round_and_panel_files_do_not_count(tmp_path):
    store = _store(tmp_path, 7, ["review-1.txt", "review-3.txt", "review-4-1.txt", "score-3-1.txt", "notes.txt"],
                   at=START + timedelta(minutes=1))
    runs = [_run(7), _run(8), _run(9, phase="ci")]
    assert watch_data.review_rounds(store, runs, _journal(7, 1, 2, 3)) == {7: 4, 8: 1}


def test_a_saved_round_still_being_scored_is_the_current_round(tmp_path):
    # Single review and the panel's merge both save review-6.txt before scoring and journaling.
    store = _store(tmp_path, 1, [f"review-{k}.txt" for k in range(1, 7)], at=START + timedelta(minutes=1))
    assert watch_data.review_rounds(store, [_run(1)], _journal(1, 1, 2, 3, 4, 5)) == {1: 6}
    assert watch_data.review_rounds(store, [_run(1)], _journal(1, 1, 2, 3, 4, 5, 6)) == {1: 7}


def test_panel_members_answering_before_the_merge_keep_the_round(tmp_path):
    store = _store(tmp_path, 1, ["review-1.txt", "review-2-1.txt", "review-2-2.txt"],
                   at=START + timedelta(minutes=1))
    assert watch_data.review_rounds(store, [_run(1)], _journal(1, 1)) == {1: 2}


def test_a_round_an_earlier_run_left_undecided_is_passed_over_like_the_runner_does(tmp_path):
    store = _store(tmp_path, 1, ["review-1.txt", "review-2.txt"], at=START - timedelta(hours=1))
    assert watch_data.review_rounds(store, [_run(1)], _journal(1, 1)) == {1: 3}


def test_an_orchestrator_verdict_takes_no_reviewer_round(tmp_path):
    # An orchestrator PASS is journaled with a round number but saves no review file; the
    # repair's reviewer round is still the runner's first (review_single.next_review_round).
    store = _store(tmp_path, 1, [])
    orchestrator = [{"kind": "review", "issue": 1, "round": 1, "verdict": "PASS", "source": "orchestrator"}]
    assert watch_data.review_rounds(store, [_run(1)], orchestrator) == {1: 1}
    _store(tmp_path, 1, ["review-1.txt"], at=START + timedelta(minutes=1))
    assert watch_data.review_rounds(store, [_run(1)], orchestrator) == {1: 1}
    decided = [*orchestrator, {"kind": "review", "issue": 1, "round": 1, "verdict": "FAIL"}]
    assert watch_data.review_rounds(store, [_run(1)], decided) == {1: 2}


def test_an_older_undecided_round_does_not_hold_back_a_later_decided_one(tmp_path):
    store = _store(tmp_path, 1, ["review-1.txt", "review-2.txt", "review-3.txt"], at=START + timedelta(minutes=1))
    assert watch_data.review_rounds(store, [_run(1)], _journal(1, 1, 3)) == {1: 4}


def test_an_unreadable_journal_leaves_the_round_unknown(tmp_path):
    store = _store(tmp_path, 1, ["review-1.txt"])
    assert watch_data.review_rounds(store, [_run(1)], None) == {}
