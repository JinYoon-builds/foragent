"""Acceptance test for init-style-skills. Locked by Foragent; do not edit."""

from pathlib import Path
import re


SKILL = Path(__file__).resolve().parents[2] / "skills/fa-init/SKILL.md"


def skills_step():
    text = SKILL.read_text(encoding="utf-8")
    step = re.search(r"^9\.\s+(.*?)(?=^\d+\.\s|\Z)", text, re.M | re.S)
    assert step, "Keep the init skills guidance in step 9"
    return " ".join(step[1].casefold().split())


def test_init_style_skills_only_chosen_style_is_fixed_on_lanes():
    text = skills_step()
    for phrase in ("style skill", "only", "design direction", "frontend-design", "fa lanes skills"):
        assert phrase in text, f"Step 9 must describe choosing only a style skill: {phrase}"


def test_init_style_skills_other_skills_belong_to_lane_agents_per_issue():
    text = skills_step()
    for phrase in ("per issue", "lane agent", "tool", "review", "domain"):
        assert phrase in text, f"Step 9 must leave nonstyle skills to agents per issue: {phrase}"


def test_init_style_skills_terminal_ui_counts_as_a_screen():
    text = skills_step()
    for phrase in ("tui", "screen", "web"):
        assert phrase in text, f"Style selection must cover web and terminal screens: {phrase}"


def test_init_style_skills_without_a_screen_skips_design_direction():
    text = skills_step()
    assert any(phrase in text for phrase in ("no screen", "without a screen", "without any screen")), (
        "Step 9 must cover projects without a screen"
    )
    assert "skip" in text and "design direction" in text, (
        "Projects without a screen must skip the design direction question"
    )
