"""Acceptance test for orch-repeat-hold. Locked by Foragent; do not edit."""

from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md"


def normalized():
    return " ".join(SKILL.read_text(encoding="utf-8").lower().split())


def test_a_second_hold_of_the_same_defect_class_fixes_the_issue_instead_of_retrying():
    text = normalized()
    for phrase in (
        "second time for the same defect class",
        "do not retry with another hint",
        "order edit",
        "split",
        "one writer per state",
    ):
        assert phrase in text, phrase


def test_escalation_is_only_for_a_product_decision_and_counting_is_by_defect_class():
    text = normalized()
    assert "only when that fix needs a product decision" in text
    assert "by defect class" in text and "not by reason code" in text
    # The old rule sent every repeated problem to a person after two attempts (D71 conflict).
    assert "after two attempts, use `fa orch escalate`" not in text


def test_a_daily_retrospective_finds_lost_run_time_and_repeated_failures():
    text = normalized()
    assert "once a day" in text and "24 h" in text
    for phrase in ("fa orch facts", "fa orch log", "where run time was lost", "architect"):
        assert phrase in text, phrase
    # Fix what is within the orchestrator's authority, tell the person one line,
    # and ask only for product decisions with choices and a recommendation.
    assert "one line to the person" in text


def test_every_retrospective_asks_whether_the_architect_is_needed():
    text = normalized()
    assert "does this need the architect" in text
    # The four signals the answer is read from (fa orch facts and the log).
    for signal in ("overlap waits", "hot files", "two writers", "check-then-act", "line limit", "idle"):
        assert signal in text, signal
    assert "fa orch architect --why" in text
    assert "record why not" in text
