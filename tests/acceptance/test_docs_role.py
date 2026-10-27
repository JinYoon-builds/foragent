"""Acceptance test for docs-role. Locked by Foragent; do not edit."""

from pathlib import Path
import re

import pytest
import yaml

from test_role_agents import frontmatter, normalized


ROOT = Path(__file__).resolve().parents[2]


def test_docs_skill_defines_owner_duties_and_orchestrator_handoff():
    path = ROOT / "skills/fa-docs/SKILL.md"
    assert path.is_file(), "Ship skills/fa-docs/SKILL.md for the standing docs owner"
    metadata, body = frontmatter(path)
    assert metadata.get("name") == "fa-docs", "Name the docs owner's skill fa-docs"
    text = normalized(body)
    requirements = (
        (r"docs? review.{0,180}(?:flag|not updated|outdated|stale)|"
         r"(?:flag|not updated|outdated|stale).{0,180}docs? review", "fix docs flagged as not updated by the docs review perspective"),
        (r"(?:fix|update|repair).{0,120}docs?|docs?.{0,120}(?:fix|update|repair)", "fix the flagged documentation"),
        (r"(?:prune|remove|delete).{0,80}(?:stale|outdated|obsolete)", "prune stale content"),
        (r"(?:merge|consolidat)\w*.{0,80}duplicat", "merge duplicates"),
        (r"(?:each|every).{0,30}fact.{0,80}(?:one|single) place|"
         r"(?:one|single) (?:source|place).{0,80}(?:each|every) fact", "keep each fact in one place"),
        (r"(?:each|every|one).{0,35}doc\w*.{0,80}one owner", "give each doc one owner"),
        (r"(?:each|every|one).{0,35}doc\w*.{0,120}one purpose", "give each doc one purpose"),
        (r"(?:follow|use|according to).{0,80}docs/voice\.md", "follow docs/voice.md"),
        (r"docs/\*\*", "own docs/**"),
        (r"readme\*", "own README*"),
        (r"(?:docs?|documentation) sections?.{0,60}skills?|skills?.{0,60}(?:docs?|documentation) sections?",
         "own the docs sections of skills"),
        (r"nothing to change.{0,80}valid|valid.{0,80}nothing to change", "allow nothing to change as a valid outcome"),
        (r"propos\w*.{0,140}orchestrat|orchestrat.{0,140}propos", "send proposals to the orchestrator"),
        (r"never.{0,80}(?:person|user|human)|(?:only|always).{0,80}(?:through|via|to) the orchestrator",
         "route communication through the orchestrator, never directly to the person"),
    )
    for pattern, duty in requirements:
        assert re.search(pattern, text), f"fa-docs must {duty}"


def test_docs_default_signals_cover_docs_and_readme():
    config = yaml.safe_load((ROOT / ".foragent.yml").read_text(encoding="utf-8"))
    docs = config.get("roles", {}).get("docs")
    assert isinstance(docs, dict), "Declare the default docs role in this repository's .foragent.yml"
    signals = docs.get("signals")
    assert isinstance(signals, list), "The docs role must declare its default call signals"
    paths = {path for signal in signals for path in signal.get("paths", [])}
    assert {"docs/**", "README*"} <= paths, "Docs signals must include docs/** and README*"


@pytest.mark.parametrize("relative", (".claude/agents/fa-docs.md", ".claude/agent-memory/fa-docs/MEMORY.md"),
                         ids=("agent", "memory"))
def test_repository_ships_docs_agent_and_project_memory(relative):
    path = ROOT / relative
    assert path.is_file(), f"Ship the standing docs role file: {relative}"
    if relative.endswith("fa-docs.md"):
        metadata, body = frontmatter(path)
        assert metadata.get("name") == "fa-docs" and metadata.get("memory") == "project"
        assert "fa-docs" in metadata.get("skills", []), "Attach the docs owner's skill"
        assert len(body.splitlines()) <= 15, "Keep the docs agent thin; its procedure belongs in the skill"
