"""Acceptance tests for n96-order-parent-docs: order and docs skills point to order parent issues."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
ORDER_SKILL = ROOT / "skills/fa-order/SKILL.md"
DOCS_SKILL = ROOT / "skills/fa-docs/SKILL.md"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("`", "")).lower()


def _gather_section() -> str:
    text = ORDER_SKILL.read_text(encoding="utf-8")
    match = re.search(r"^## 1\. Gather before asking\n(.*?)(?=^## )", text, re.M | re.S)
    assert match, "The order skill keeps its '## 1. Gather before asking' section"
    return match.group(1)


def _gather_instruction() -> str:
    """The paragraph that tells the reader what to read before asking."""
    section = _gather_section()
    paragraph = section.split("\n- ", 1)[0]
    assert "fa order context --json" in paragraph, "The gather instruction still starts from the context command"
    return _flat(paragraph)


def _duty_four() -> str:
    text = DOCS_SKILL.read_text(encoding="utf-8")
    match = re.search(r"^4\. \*\*One owner, one purpose\.\*\*(.*?)(?=^5\. \*\*)", text, re.M | re.S)
    assert match, "The docs skill keeps duty 4 'One owner, one purpose'"
    return _flat(match.group(1))


def test_gather_instruction_points_to_earlier_order_parent_issues():
    paragraph = _gather_instruction()
    assert "docs/specs" not in paragraph, (
        "The gather instruction must not send readers to the removed docs/specs/ directory")
    assert not re.search(r"existing specs?\b", paragraph), (
        "The gather instruction must not ask readers for existing spec files")
    assert re.search(r"(?:earlier|previous|past|prior|older)\s+order\s+parent\s+issues?", paragraph), (
        "The gather instruction must direct readers to earlier order parent issues")


def test_gather_section_no_longer_names_spec_directory():
    section = _flat(_gather_section())
    assert "docs/specs" not in section, (
        "The gather-before-asking section must not point readers to docs/specs/")


def test_docs_duty_four_names_markers_parent_issues_and_legacy_link_file():
    duty = _duty_four()
    assert "published order spec" not in duty, (
        "Duty 4 must not describe decision links as coming from published order specs")
    assert "docs/specs" not in duty, "Duty 4 must not name the removed docs/specs directory"
    sources = re.search(r"fa decisions --json.{0,400}", duty)
    assert sources, "Duty 4 still explains how fa decisions --json derives links"
    window = sources.group(0)
    assert re.search(r"decision markers?", window), (
        "Duty 4 must name issue decision markers as a decision-link source")
    assert re.search(r"order parent issues?", window), (
        "Duty 4 must name order parent issues as a decision-link source")
    assert "docs/decision-links.json" in duty, (
        "Duty 4 must name docs/decision-links.json")


def test_docs_duty_four_ties_link_file_to_older_orders_only():
    duty = _duty_four()
    index = duty.find("docs/decision-links.json")
    assert index >= 0, "Duty 4 must name docs/decision-links.json"
    around = duty[max(0, index - 160): index + 160]
    assert re.search(r"\b(?:older|earlier|legacy|previous|past|prior)\b.{0,40}orders?|orders?.{0,40}"
                     r"\b(?:before|older|earlier|legacy)\b", around), (
        "Duty 4 must explain docs/decision-links.json as the source for older orders' links")
    assert re.search(r"decided, nothing ordered", duty) and re.search(r"do not write status by hand", duty), (
        "Duty 4 keeps the derived-status explanation around the new sources")
