"""Locked text acceptance for lane agents. Break-it: reject a safe template
with stale shipped lane agents, or instructions that leave missing commands to a person.
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


@pytest.mark.parametrize("lane", ["sample", "another-lane"])
def test_generated_and_shipped_lane_agents_route_github_writes(lane):
    from foragent.lanes import agent_text

    text = agent_text(lane, [f"{lane}/**"], [])
    assert_no_bare_gh_writes(text, f"generated {lane}")
    assert_fa_policy(text, f"generated {lane}")
    roles = {"fa-orchestrator", "fa-architect", "fa-docs"}
    agents = [path for path in sorted((ROOT / ".claude/agents").glob("fa-*.md"))
              if path.stem not in roles]
    assert agents, "Ship lane definitions as well as their template"
    for path in agents:
        text = path.read_text(encoding="utf-8")
        assert_no_bare_gh_writes(text, path.relative_to(ROOT))
        assert_fa_policy(text, path.relative_to(ROOT))
