"""Review rounds follow the runner's journaled verdict, not a re-parse of the raw text."""

from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace

from foragent import orch_events
from foragent.orch_facts_reviews import attach, round_classes
from foragent.state import StateStore

START = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
FINDING = "VERDICT: FAIL\n- src/a.py:3 — The retry loses context.\n  class: retry-context\n"


def _store(tmp_path):
    return StateStore(tmp_path / "state", now=lambda: START)


def _review(store, number, round_number, raw, at, verdict=None, **fields):
    path = store.root / "runs" / f"issue-{number}" / f"review-{round_number}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    os.utime(path, (at.timestamp(), at.timestamp()))
    if verdict is not None:  # The runner journals right after it saves the file.
        orch_events.append_event(store, at + timedelta(milliseconds=5), number, "review", f"{verdict} 0 findings",
                                 verdict=verdict, findings=0, round=round_number, source="reviewer", **fields)


def _attach(store, number=7):
    execution = SimpleNamespace(run=SimpleNamespace(number=number), started=START)
    rows = [{"issue": number, "status": "finished", "outcome": "merged", "started_at": START.isoformat()}]
    return attach(store, [execution], rows), rows[0]


def test_unusable_journaled_round_with_a_verdict_line_counts_nothing(tmp_path):
    store = _store(tmp_path)
    # A timed-out reviewer, or a failed panel's "Reviewer 1:\nVERDICT: FAIL ...", still has a VERDICT line.
    _review(store, 7, 1, FINDING, START + timedelta(seconds=10), verdict="UNUSABLE")
    _review(store, 7, 2, "Reviewer 1:\n" + FINDING + "\nReviewer 2 of 2 gave no verdict\n",
            START + timedelta(seconds=20), verdict="UNUSABLE")
    _review(store, 7, 3, "VERDICT: PASS\n", START + timedelta(seconds=30), verdict="PASS")
    summary, row = _attach(store)
    assert row["review_rounds"] == 1 and row["finding_classes"] == {}
    assert summary["finding_classes"] == {}


def test_journaled_verdict_decides_over_the_text(tmp_path):
    store = _store(tmp_path)
    _review(store, 7, 1, "not a verdict at all\n", START + timedelta(seconds=10), verdict="FAIL")
    _review(store, 7, 2, FINDING, START + timedelta(seconds=20), verdict="FAIL")
    _review(store, 7, 3, "VERDICT: FAIL\n- src/b.py:1 — Gone.\n", START + timedelta(seconds=30), verdict="PASS")
    summary, row = _attach(store)
    assert row["review_rounds"] == 3
    assert row["finding_classes"] == {"unclassified": 1, "retry-context": 1}


CONFLICT = "SPEC-CONFLICT: tests/acceptance/test_x.py:1 — wants the opposite\n"


def test_conflict_round_counts_the_reviewers_own_verdict(tmp_path):
    store = _store(tmp_path)
    _review(store, 7, 1, FINDING + CONFLICT, START + timedelta(seconds=10),
            verdict="SPEC-CONFLICT", review_verdict="FAIL")
    _review(store, 7, 2, FINDING + CONFLICT, START + timedelta(seconds=20),
            verdict="SPEC-CONFLICT", review_verdict="UNUSABLE")
    _review(store, 7, 3, "VERDICT: PASS\n" + CONFLICT, START + timedelta(seconds=30),
            verdict="SPEC-CONFLICT", review_verdict="PASS")
    # Journaled before the reviewer's verdict was kept: judged from the text, conflict lines excluded.
    _review(store, 7, 4, "VERDICT: FAIL\n" + CONFLICT, START + timedelta(seconds=40), verdict="SPEC-CONFLICT")
    _, row = _attach(store)
    assert row["review_rounds"] == 3
    assert row["finding_classes"] == {"retry-context": 1, "unclassified": 1}


def test_earlier_orchestrator_verdict_of_the_same_round_number_does_not_hide_the_review(tmp_path):
    store = _store(tmp_path)
    for seconds in (5, 59.8):  # Long before, and within a second of, the reviewer's own file.
        orch_events.append_event(store, START + timedelta(seconds=seconds), 7, "review", "PASS 0 findings",
                                 verdict="PASS", findings=0, round=1, source="orchestrator")
    _review(store, 7, 1, FINDING, START + timedelta(seconds=60), verdict="UNUSABLE")
    _, row = _attach(store)
    assert row["review_rounds"] == 0


def test_unjournaled_legacy_review_is_judged_from_the_text(tmp_path):
    store = _store(tmp_path)
    _review(store, 7, 1, FINDING, START + timedelta(seconds=10))
    _review(store, 7, 2, "no verdict here\n", START + timedelta(seconds=20))
    _, row = _attach(store)
    assert row["review_rounds"] == 1 and row["finding_classes"] == {"retry-context": 1}


def test_json_findings_read_the_class_field():
    text = '```json\n[{"file": "a.py", "line": 3, "summary": "x", "class": "stale-base"},' \
           ' {"file": "b.py", "line": 1, "summary": "y"}]\n```'
    assert round_classes(text) == {"stale-base", "unclassified"}
    assert round_classes(text, "UNUSABLE") is None
    assert round_classes("(none)") == set()
