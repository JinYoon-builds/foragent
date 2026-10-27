"""Acceptance test for role-guard. Locked by Foragent; do not edit."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def norm(text):
    return " ".join(text.lower().split())


def test_planning_skills_try_the_lightest_fix_before_a_new_role():
    # Jin (2026-10-27): roles must not multiply; propose the cheaper fit first.
    for path in ("skills/fa/SKILL.md", "skills/fa-order/SKILL.md"):
        text = norm((ROOT / path).read_text(encoding="utf-8"))
        assert "fa role add" in text, path
        order = [text.find(phrase) for phrase in (
            "lane memory", "skill attached to the lane", "reviewer perspective", "new role")]
        assert all(index >= 0 for index in order), (path, order)
        assert order == sorted(order), f"{path}: cheaper options must come before a new role"
        assert "cheaper option first" in text, path
        assert "roles never message the person" in text, path
