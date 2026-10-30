"""Acceptance test for rules-readers-order. Locked by Foragent; do not edit."""

from pathlib import Path


ROOT = next(path for path in Path(__file__).resolve().parents
            if (path / ".foragent.yml").is_file())


def test_change_step_updates_history_and_current_rule_in_the_same_approved_order():
    skill = (ROOT / "skills/fa-order/SKILL.md").read_text(encoding="utf-8")
    change = " ".join(skill.split("- For Change,", 1)[1].split("\n\n", 1)[0].lower().split())

    assert "docs/rules.md" in change, "The Change step must update the rule the readers actually use"
    assert "docs/decisions.md" in change
    assert "append" in change and "dated" in change and "reason" in change
    assert "replace" in change and "line" in change
    assert "same approved order" in change, "History and the current rule change together under the existing approval"
