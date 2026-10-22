"""Acceptance test for fa-order-interview. Locked by Foragent; do not edit."""

from pathlib import Path
import re


def interview_text():
    path = Path(__file__).resolve().parents[2] / "skills/fa-order/SKILL.md"
    return " ".join(path.read_text(encoding="utf-8").casefold().split())


def test_fa_order_interview_builds_decision_ledger_from_docs_before_one_confirmation():
    text = interview_text()
    assert "decision ledger" in text
    assert re.search(r"(?:build|compile|extract|read|gather).{0,180}(?:docs|documents)", text)
    assert re.search(r"(?:before|prior to).{0,80}ask", text)
    assert re.search(r"(?:once|one time|single).{0,80}confirm|confirm.{0,80}(?:once|one time|single)", text)


def test_fa_order_interview_current_words_win_and_conflict_changes_update_record():
    text = interview_text()
    assert re.search(r"(?:current|latest).{0,80}(?:words|request|instruction).{0,120}(?:win|precedence|override|priority)", text)
    for choice in ("change", "keep", "exception this once"):
        assert choice in text
    assert re.search(r"(?:change|update).{0,120}decision (?:record|ledger)", text)


def test_fa_order_interview_asks_one_least_clear_question_with_source_labels():
    text = interview_text()
    assert "one question at a time" in text
    assert "least clear" in text or "least certain" in text
    for source in ("code", "docs", "person", "assumption"):
        assert re.search(rf"(?:source|label).{{0,180}}\b{source}\b", text), source


def test_fa_order_interview_discovers_skills_and_leaves_per_issue_choices_to_agents():
    text = interview_text()
    for command in ("fa skills add", "gh search", "fa profile set"):
        assert command in text
    for fact in ("license", "stars", "scripts"):
        assert fact in text
    assert re.search(r"style.{0,140}(?:confirm|approv).{0,100}(?:person|user|human)", text)
    assert re.search(r"(?:lane agents?|agents?).{0,140}(?:pick|choos|select).{0,140}(?:per issue|each issue)", text)


def test_fa_order_interview_native_path_has_voice_and_no_manual_sync_guidance():
    text = interview_text()
    assert "fa skills sync" not in text
    assert re.search(r"(?:default|by default).{0,100}native|native.{0,100}(?:default|by default)", text)
    # An optional engine branch may still document the external interview commands.
    for match in re.finditer(r"ooo\s|ouroboros interview", text):
        nearby = text[max(0, match.start() - 300):match.start()]
        assert "interview.engine" in nearby or re.search(r"if.{0,100}ouroboros|only.{0,100}ouroboros", nearby)
    assert "voice" in text and "docs/voice.md" in text
