"""Acceptance test for orch-ask-rule. Locked by Foragent; do not edit."""

from pathlib import Path
import re


def ask_rule_text():
    return " ".join((Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md").read_text().lower().split())


def test_orch_ask_rule_limits_questions_to_d71_human_choices():
    text = ask_rule_text()
    assert "d71" in text
    assert re.search(r"(?:ask|question).{0,100}only|only.{0,100}(?:ask|question)", text)
    for concept in (r"goal.{0,40}chang|chang.{0,40}goal", r"trade.off", r"taste|preference",
                    r"hard.to.undo|irreversible|difficult to undo", r"scope", r"priorit"):
        assert re.search(concept, text), concept
    for boundary in ("delete", "deploy", "public", "always_human", "risk:high"):
        assert boundary in text, boundary


def test_orch_ask_rule_answerable_repairs_are_done_then_reported_with_record():
    text = ask_rule_text()
    for concept in (r"(?:own|wrote).{0,120}tests?.{0,160}(?:document|behaviour|behavior)",
                    r"(?:safety|guard).{0,100}(?:misfire|false positive|mistake|misjudg)", r"plumbing"):
        assert re.search(concept, text), concept
    assert re.search(r"(?:facts|evidence).{0,100}(?:confirm|verif)|(?:confirm|verif).{0,100}(?:facts|evidence)", text)
    assert re.search(r"(?:do|fix|repair|act).{0,80}(?:first|then|before).{0,100}(?:report|tell|inform)", text)
    assert re.search(r"(?:one.line|single.line).{0,100}(?:record|log)|(?:record|log).{0,100}(?:one.line|single.line)", text)


def test_orch_ask_rule_locked_test_owner_can_correct_through_reapproved_order_edit():
    text = ask_rule_text()
    assert re.search(r"(?:implementer|worker).{0,100}(?:never|must not|cannot|may not).{0,80}(?:edit|change).{0,80}(?:locked|acceptance)", text)
    assert re.search(r"(?:test owner|orchestrator).{0,160}(?:correct|fix).{0,120}(?:own|tests)", text)
    assert re.search(r"order edit.{0,120}re.approval|re.approval.{0,120}order edit", text)
    # A spec question can have a documented answer; the old blanket escalation contradicts D71.
    assert "spec questions, changed requirements, re-approval and taste belong to the user" not in text
    assert not re.search(r"(?:all|every|always).{0,40}spec questions?.{0,100}(?:user|human)|spec questions?.{0,60}always.{0,80}(?:user|human)", text)
