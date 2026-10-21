"""Acceptance: the /fa-init skill has a brownfield path built on `fa adopt`."""

from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills/fa-init/SKILL.md"


def test_brownfield_steps_are_present_in_order():
    text = SKILL.read_text(encoding="utf-8")
    steps = ["fa adopt survey", "fa lanes add", "fa adopt baseline", "fa adopt ci", "/fa-order"]
    positions = [text.find(step) for step in steps]
    assert all(position >= 0 for position in positions), dict(zip(steps, positions))
    assert positions == sorted(positions)


def test_ci_change_needs_user_yes_and_new_project_path_is_kept():
    text = SKILL.read_text(encoding="utf-8")
    assert "--exclude-acceptance" in text
    assert "AskUserQuestion" in text
    assert "git init" in text and "gh repo create" in text
