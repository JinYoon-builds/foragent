"""Acceptance test for architect-references. Locked by Foragent; do not edit."""

import re

import yaml

from test_vendored_prove_it_works import ROOT, normalized
from test_view_ux_copy import assert_kwp_apache_attribution


def test_architect_links_attributed_adr_and_debt_references_and_keeps_testing_strategy():
    skill = ROOT / "skills/fa-architect/SKILL.md"
    assert skill.is_file(), "Missing architect skill: skills/fa-architect/SKILL.md"
    references = skill.parent / "references"
    assert references.is_dir(), "Vendor the architect references under skills/fa-architect/references/"
    documents = {path: path.read_text(encoding="utf-8") for path in sorted(references.rglob("*"))
                 if path.is_file()}
    adr = []
    debt = []
    for path, text in documents.items():
        headings = {normalized(match[1]).strip(" #")
                    for match in re.finditer(r"^[ \t]{0,3}#{1,6}[ \t]+(.+)$", text, re.M)}
        if {"easier", "harder", "revisit"} <= headings:
            adr.append(path)
        if re.search(r"\(\s*impact\s*\+\s*risk\s*\)\s*[x×]\s*\(\s*6\s*[-−]\s*effort\s*\)",
                     normalized(text)):
            debt.append(path)
    assert adr, "Add an ADR consequences reference with the headings Easier, Harder, Revisit"
    assert debt, "Add a tech-debt scoring reference with (Impact + Risk) × (6 − Effort)"
    targets = re.findall(r"\[[^\]]+\]\(<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\)",
                         skill.read_text(encoding="utf-8"))
    linked = {(skill.parent / target.split("#", 1)[0]).resolve() for target in targets}
    linked_references = []
    for candidates, topic in ((adr, "ADR consequences"), (debt, "tech-debt scoring")):
        matching = [path for path in candidates if path.resolve() in linked]
        assert matching, f"skills/fa-architect/SKILL.md must link the {topic} reference"
        linked_references.append(matching)
        for path in matching:
            assert_kwp_apache_attribution(documents[path], str(path.relative_to(ROOT)))
    assert any(adr_path != debt_path for adr_path in linked_references[0] for debt_path in linked_references[1]), (
        "Link distinct reference files for ADR consequences and tech-debt scoring"
    )
    raw = ROOT / ".claude/skills/tech-debt"
    assert not raw.exists() and not raw.is_symlink(), "Remove the raw .claude/skills/tech-debt copy"
    assert (ROOT / ".claude/skills/testing-strategy/SKILL.md").is_file(), (
        "Keep .claude/skills/testing-strategy: the orch and order lanes still use it"
    )
    lanes = yaml.safe_load((ROOT / ".foragent.yml").read_text(encoding="utf-8"))["lanes"]
    for lane in ("orch", "order"):
        assert "testing-strategy" in (lanes[lane].get("skills") or []), (
            f"Keep testing-strategy attached to the {lane} lane"
        )
