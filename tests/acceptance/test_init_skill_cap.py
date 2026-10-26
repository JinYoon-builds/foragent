"""Acceptance test for init-skill-cap (D35). Locked by Foragent; do not edit."""

from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills/fa-init/SKILL.md"


def test_fa_init_has_no_skill_count_limit():
    text = SKILL.read_text(encoding="utf-8")
    assert "at most three" not in text
    assert "lane limit" not in text
    for name in ("frontend-design", "design-system", "design-critique",
                 "accessibility-review", "ux-copy", "webapp-testing"):
        assert name in text
