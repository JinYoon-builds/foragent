"""Reader docs follow the decisions and features merged on 2026-11-04/05."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _text(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_rules_cite_each_new_standing_decision_and_drop_the_retired_skill_rule():
    rules = _text("docs/rules.md")
    decisions = _text("docs/decisions.md")
    for number in ("D109", "D110", "D111", "D112", "D113", "D114", "D115", "D116", "D117", "D118",
                   "D119", "D120", "D122"):
        assert f"### {number}" in decisions or f"## {number}" in decisions or number in decisions, number
        assert re.search(rf"\b{number}\b", rules), f"docs/rules.md must cite {number} in its section"
    assert "D121" not in rules, "D121 is a target, not a rule in force"
    # D117 commits vendored principle skills, so the D37 line saying library skills are never committed is gone.
    assert "not committed (D37)" not in rules


def test_readme_names_ghostty_and_the_git_source_principle_in_both_languages():
    readme = _text("README.md")
    assert readme.count("Ghostty") >= 2, "the English and Korean terminal lists both name Ghostty"
    english, _, korean = readme.partition("한국어")
    assert re.search(r"(?i)git(hub)? .*source of truth|source of truth.*git", english), "English: D109 principle"
    assert re.search(r"git|GitHub", korean) and ("원본" in korean or "기준" in korean), "Korean: D109 principle"


def test_order_doc_lists_closes_among_the_publish_writes():
    order = _text("docs/order.md")
    assert "closes" in order, "docs/order.md section 6 names the plan's closes field"
