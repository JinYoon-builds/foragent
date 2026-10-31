"""Locked text acceptance for role guidance. Break-it: reject an unsafe example,
a missing FA write policy, or a missing-command fallback left to the person.
Read-only gh inspection remains available. Generated role definitions are public output.
"""

from pathlib import Path
import re

import pytest

ROOT = next(path for path in Path(__file__).resolve().parents
            if (path / "pyproject.toml").is_file() and (path / "src/foragent").is_dir())


def assert_fa_policy(text, source):
    paragraphs = [" ".join(part.split()) for part in text.split("\n\n")]
    assert any(re.search(r"\bGitHub\b", part, re.I)
               and re.search(r"\b(?:writ\w*|mutat\w*|chang\w*)\b", part, re.I)
               and re.search(r"\bfa\s+(?:commands?|[a-z-]+)", part, re.I)
               for part in paragraphs), f"{source}: explicitly route GitHub writes through FA commands"
    assert any(re.search(r"\b(?:no|none|missing|unavailable|doesn.t|not|lack\w*)\b", part, re.I)
               and re.search(r"\bcommand\w*\b", part, re.I)
               and re.search(r"\b(?:propos\w*|request\w*)\b", part, re.I)
               and re.search(r"\bissue\w*\b", part, re.I)
               for part in paragraphs), f"{source}: propose an issue when no FA command fits"


def assert_no_bare_gh_writes(text, source):
    # This is a locked text check, not a runtime classifier or an agent step order.
    writes = re.findall(
        r"\bgh\s+(?:(?:--repo|-R)\s+\S+\s+)?"
        r"(?:(?:issue|pr|repo|label|release)\s+"
        r"(?:create|edit|close|reopen|merge|comment|delete|lock|unlock|transfer)"
        r"|project\s+(?:item-edit|field-create|item-add))\b[^`\n]*",
        text, re.I,
    )
    for command in re.findall(r"\bgh\s+api\b[^`\n]*", text, re.I):
        graphql = re.search(r"\bgraphql\b", command, re.I)
        mutation = re.search(r"\bmutation\b", command, re.I)
        write_method = re.search(r"(?:-X|--method)[ =]*(?:POST|PUT|PATCH|DELETE)\b", command, re.I)
        read_method = re.search(r"(?:-X|--method)[ =]*(?:GET|HEAD)\b", command, re.I)
        fields = re.search(r"(?:^|\s)(?:-f|-F|--field|--raw-field)(?:[ =])", command)
        if mutation or (not graphql and (write_method or (fields and not read_method))):
            writes.append(command)
    assert not writes, f"{source}: bare gh write instructions remain: {writes}"


def test_orchestrator_skill_routes_writes_and_keeps_read_only_inspection():
    files = sorted(path for name in ("fa-orchestrator", "fa-architect", "fa-docs")
                   for path in (ROOT / "skills" / name).rglob("*.md"))
    assert files
    for path in files:
        assert_no_bare_gh_writes(path.read_text(encoding="utf-8"), path.relative_to(ROOT))
    skill = (ROOT / "skills/fa-orchestrator/SKILL.md").read_text(encoding="utf-8")
    assert_fa_policy(skill, "fa-orchestrator")
    dreams = (ROOT / "skills/fa-orchestrator/references/dreams.md").read_text(encoding="utf-8")
    assert "fa orch dream --redo" in dreams, "Give the orchestrator the FA recovery command"
    assert "gh pr diff" in dreams and "gh pr view" in dreams, "Keep read-only PR inspection"


@pytest.mark.parametrize("role", ["fa-orchestrator", "fa-architect", "fa-docs"])
def test_generated_and_shipped_role_agents_route_github_writes(role):
    from foragent.role_agents import agent_text

    texts = {f"generated {role}": agent_text(role),
             f"shipped {role}": (ROOT / ".claude/agents" / f"{role}.md").read_text(encoding="utf-8")}
    for source, text in texts.items():
        assert_no_bare_gh_writes(text, source)
        assert_fa_policy(text, source)
