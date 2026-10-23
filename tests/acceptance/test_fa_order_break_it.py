"""Acceptance test for fa-order-break-it. Locked by Foragent; do not edit."""

from pathlib import Path
import re

BREAK_IT = ("Make the code return a wrong value, skip an effect, or do the effect twice. "
            "If the test still passes, it proves nothing.")
ROOT = Path(__file__).resolve().parents[2]


def test_fa_order_break_it_rule_guides_writing_the_final_checks():
    text = (ROOT / "skills/fa-order/SKILL.md").read_text()
    flat = " ".join(text.split())
    assert BREAK_IT in flat, "fa-order must judge each acceptance test by the break-it rule"
    section = re.search(r"## 5\..*?(?=\n## |\Z)", text, re.S)
    assert section and BREAK_IT in " ".join(section[0].split()), (
        "Put the rule where the final checks are written (step 5)")


def test_fa_order_break_it_rule_is_credited_to_its_source():
    flat = " ".join((ROOT / "skills/fa-order/SKILL.md").read_text().split())
    assert re.search(r"pstack|principle-test-behavior-not-implementation", flat), (
        "Name the pstack principle the rule comes from (MIT)")
