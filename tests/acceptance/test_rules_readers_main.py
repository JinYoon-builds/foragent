"""Acceptance test for rules-readers-main. Locked by Foragent; do not edit."""

from pathlib import Path
import re

from foragent import order_perspectives


ROOT = next(path for path in Path(__file__).resolve().parents
            if (path / ".foragent.yml").is_file())


def test_incidents_receives_all_rules_without_history_or_cut_notice(git_history):
    root = git_history.clone
    docs = root / "docs"
    docs.mkdir()
    # Exceed the old decision-entry window to catch retaining that cut for rules.
    rules = "# Rules\n\n" + "".join(
        f"### D{number}. Current rule {number}\nRULE_CURRENT_BODY_{number:02d}\n\n"
        for number in range(1, 26)
    )
    (docs / "rules.md").write_text(rules, encoding="utf-8")
    (docs / "decisions.md").write_text("HISTORY_MUST_NOT_REACH_INCIDENTS\n", encoding="utf-8")
    (root / ".foragent-risks.yml").write_text("risks: [RISK_CONTEXT_STAYS]\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in docs.iterdir()}

    prompt = order_perspectives.prompt("incidents", {"id": "rules-reader"}, root)

    assert rules.strip() in prompt, "Incidents must receive docs/rules.md whole, including its first rule"
    assert "docs/rules.md" in prompt
    assert "HISTORY_MUST_NOT_REACH_INCIDENTS" not in prompt
    assert "Cut notice:" not in prompt, "The latest-20 notice applies only to the decisions.md fallback"
    assert "RISK_CONTEXT_STAYS" in prompt
    assert before == {path: path.read_bytes() for path in docs.iterdir()}


def test_incidents_without_rules_keeps_decision_titles_and_latest_twenty(git_history):
    root = git_history.clone
    docs = root / "docs"
    docs.mkdir()
    (docs / "decisions.md").write_text("# Decisions\n\n" + "".join(
        f"### D{number}. Historical decision {number}\nHISTORY_BODY_{number:02d}\n\n"
        for number in range(1, 26)
    ), encoding="utf-8")

    prompt = order_perspectives.prompt("incidents", {"id": "adopted-project"}, root)

    assert "docs/decisions.md" in prompt
    assert "Cut notice: docs/decisions.md has 25 decisions" in prompt
    assert all(f"### D{number}. Historical decision {number}" in prompt for number in range(1, 26))
    assert all(f"HISTORY_BODY_{number:02d}" not in prompt for number in range(1, 6))
    assert all(f"HISTORY_BODY_{number:02d}" in prompt for number in range(6, 26))


def test_existing_empty_rules_does_not_reenable_history(git_history):
    docs = git_history.clone / "docs"
    docs.mkdir()
    (docs / "rules.md").write_text("", encoding="utf-8")
    (docs / "decisions.md").write_text("HISTORY_AFTER_RULES_CLEARED\n", encoding="utf-8")

    prompt = order_perspectives.prompt("incidents", {}, git_history.clone)

    assert "HISTORY_AFTER_RULES_CLEARED" not in prompt, "Fallback is for a missing file, not an empty rules file"
    assert "docs/rules.md" in prompt


def test_docs_skill_separates_current_rules_from_history_and_keeps_rules_short_at_finish():
    raw = (ROOT / "skills/fa-docs/SKILL.md").read_text(encoding="utf-8").lower()
    skill = " ".join(raw.split())
    paragraphs = [" ".join(paragraph.split()) for paragraph in raw.split("\n\n")]

    assert "docs/rules.md" in skill, "The docs owner must own the current-rules document"
    assert "rules in force now" in skill
    assert "docs/decisions.md" in skill and "append-only history" in skill
    assert any(all(word in paragraph for word in ("rules.md", "short", "order", "finish"))
               for paragraph in paragraphs), "Keeping rules short must be an explicit duty at order finish"
    assert any(all(word in paragraph for word in ("rules.md", "replace", "line"))
               for paragraph in paragraphs), "Superseded rules are replaced in the current-rules document"


def test_readme_links_current_rules_and_retains_history_link():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert re.search(r"\[[^\]]+\]\(docs/rules\.md\)", readme), "The README document list must link docs/rules.md"
    assert "(docs/decisions.md)" in readme
