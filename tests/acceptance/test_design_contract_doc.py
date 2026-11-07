"""Acceptance tests for issue design-contract-doc (r466).

docs/design/CONTRACT.md holds the five dashboard rules as product-wide
principles, and the fa-order skill's pointer to it resolves.
"""

import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "docs/design/CONTRACT.md"
ORDER_SKILL = ROOT / "skills/fa-order/SKILL.md"

# One pattern list per rule, in the issue's order. Each rule item must match
# every pattern of its own rule (English or Korean wording is accepted).
RULES = [
    ("one screen shows one piece of information",
     [r"(one screen|single screen|each screen|한 화면)", r"(one piece of information|one thing|one kind of information|정보 하나|하나만|one piece)"]),
    ("details sit behind the acorn click (bubble)",
     [r"(acorn|도토리)", r"(bubble|말풍선)", r"(detail|상세|자세한|이슈 하나)"]),
    ("the r53 layout",
     [r"\br53\b"]),
    ("Korean only in the person-facing view",
     [r"(Korean|한국어|한글)", r"(person-facing|people-facing|human-facing|사람(이|에게)? ?보는|사람용)"]),
    ("no person's name on screen",
     [r"(name|이름)", r"(\bno\b|never|not|without|않|없|금지|두지)"]),
]


def contract_text():
    assert CONTRACT.is_file(), "docs/design/CONTRACT.md does not exist"
    return CONTRACT.read_text(encoding="utf-8")


def rules_section(text):
    match = re.search(r"^## Rules[ \t]*\n(.*?)(?=^#{1,2} |\Z)", text, re.M | re.S)
    assert match, "CONTRACT.md has no '## Rules' section"
    return match.group(1)


def numbered_items(section):
    items = []
    for line in section.splitlines():
        start = re.match(r"^(\d+)[.)]\s+(.*)$", line)
        if start:
            items.append([int(start.group(1)), start.group(2)])
        elif items and line.strip():
            items[-1][1] += " " + line.strip()
    return items


def matches_rule(item_text, patterns):
    return all(re.search(p, item_text, re.I) for p in patterns)


def test_order_context_reports_this_repository_has_a_design_foundation(monkeypatch, capsys):
    from foragent import cli
    from foragent.tracker import FakeTracker

    monkeypatch.setattr(cli, "GhTracker", lambda repo: FakeTracker())
    assert cli.main(["order", "context", "--repo-root", str(ROOT), "--json"]) == 0
    ui = json.loads(capsys.readouterr().out)["ui"]
    assert ui["has_design_foundation"] is True


def test_contract_has_exactly_five_numbered_rules_in_the_issue_order():
    items = numbered_items(rules_section(contract_text()))
    assert [number for number, _ in items] == [1, 2, 3, 4, 5], items
    for index, (name, patterns) in enumerate(RULES):
        text = items[index][1]
        assert matches_rule(text, patterns), f"rule {index + 1} does not state '{name}': {text!r}"


def test_each_rule_is_stated_once_and_only_in_its_own_item():
    items = numbered_items(rules_section(contract_text()))
    assert len(items) == 5
    # The distinctive marker of each rule appears in its own item and is not
    # repeated as a second rule item elsewhere.
    for index, (name, patterns) in enumerate(RULES):
        owners = [i for i, (_, text) in enumerate(items) if matches_rule(text, patterns)]
        assert index in owners, f"rule '{name}' missing from item {index + 1}"
    r53_items = [i for i, (_, text) in enumerate(items) if re.search(r"\br53\b", text)]
    assert r53_items == [2], r53_items


def test_contract_cites_d112_and_says_lane_memory_is_not_the_source():
    text = contract_text()
    assert re.search(r"\bD112\b", text), "CONTRACT.md does not cite D112"
    normalized = " ".join(text.split())
    memory = re.search(
        r"[^.。\n]*(lane memory|lane memories|레인 기억)[^.。\n]*", normalized, re.I)
    assert memory, "CONTRACT.md has no sentence about lane memory"
    sentence = memory.group(0)
    assert re.search(r"(source|원본|원천)", sentence, re.I), sentence
    assert re.search(r"(\bnot\b|아니|않)", sentence, re.I), sentence


def test_fa_order_skill_pointer_resolves_to_the_contract():
    skill = ORDER_SKILL.read_text(encoding="utf-8")
    pointers = re.findall(r"docs/design/CONTRACT\.md", skill)
    assert pointers, "fa-order skill no longer points at docs/design/CONTRACT.md"
    # The pointer is relative to the repository root; it must name an existing file.
    target = ROOT / "docs/design/CONTRACT.md"
    assert target.is_file(), "fa-order's docs/design/CONTRACT.md pointer does not resolve"
    assert target.resolve() == CONTRACT.resolve()
    assert "## Rules" in target.read_text(encoding="utf-8")
