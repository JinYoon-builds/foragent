"""Acceptance test for state-design-skill. Locked by Foragent; do not edit."""

import re

from test_vendored_prove_it_works import ROOT, assert_lane_skills, frontmatter, normalized


def test_state_design_skill_covers_general_state_ownership():
    skill = ROOT / "skills/fa-state-design/SKILL.md"
    metadata, body = frontmatter(skill)
    assert metadata.get("name") == "fa-state-design", "Name the product skill fa-state-design"
    text = normalized(body)
    for phrase in ("one writer per piece of state", "derive instead of store", "append instead of overwrite"):
        assert phrase in text, f"fa-state-design must cover: {phrase}"
    assert any(phrase in text for phrase in ("check-then-act", "check then act", "toctou")), (
        "fa-state-design must cover check-then-act windows"
    )
    assert re.search(r"\b(?:windows?|races?|atomic\w*|locks?|transactions?)\b", text), (
        "Explain the race window or atomicity needed for check-then-act"
    )
    assert re.search(r"\bduplicat\w* state\b", text), "Flag duplicated state and its consistency cost"
    assert re.search(r"\breaders?\b", text) and "lag" in text and re.search(r"\b(?:tolerat|accept)\w*\b", text), (
        "Name which reader tolerates the lag between duplicated state copies"
    )
    assert "fallback writer" in text and re.search(
        r"\bonly (?:after|when) (?:the )?owner (?:is |has been )?confirmed dead\b"
        r"|\bonly after confirming (?:that )?(?:the )?owner is dead\b", text,
    ), "A fallback writer acts only after the owner is confirmed dead"
    assert re.search(
        r"\bplans?\b[^.]*\bwriter\b[^.]*\b(?:each|every)\b[^.]*\bstate\b"
        r"|\bplans?\b[^.]*\b(?:each|every)\b[^.]*\bstate\b[^.]*\bwriter\b", text,
    ), "A plan must state the writer of each piece of state"
    assert not re.search(r"#\d+", skill.read_text(encoding="utf-8")), (
        "Keep fa-state-design general: remove Foragent issue numbers"
    )


def test_state_design_is_attached_to_orch_skills_and_board():
    assert_lane_skills("fa-state-design", ("orch", "skills", "board"))
