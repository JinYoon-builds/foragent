"""Check shipped current guidance and rendered string literals, not code names.

decisions.md and dated specs/research are history, not current operating guidance.
They remain append-only evidence (D108). Comments/docstrings and identifier names
are also not person-facing text. No substring exemption hides current guidance.
"""

import ast
from pathlib import Path

import pytest

from foragent import cli


def public_literals(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)
                  and isinstance(node.body[0].value.value, str)}
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]


def test_current_person_facing_sources_use_pm_everywhere():
    root = Path(cli.__file__).resolve().parents[2]
    documents = [*root.glob("README*"), *root.glob("skills/**/*.md"), *root.glob(".claude/agents/*.md")]
    documents += [path for path in (root / "docs").rglob("*.md")
                  if path.name != "decisions.md"
                  and not {"specs", "research", "archive"}.intersection(path.relative_to(root / "docs").parts)]
    hits = []
    for path in sorted(set(documents)):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(word in line.casefold() for word in ("planning session", "기획 세션")):
                hits.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    for path in sorted((root / "src/foragent").rglob("*.py")):
        for number, value in public_literals(path):
            if any(word in value.casefold() for word in ("planning session", "기획 세션")):
                hits.append(f"{path.relative_to(root)}:{number}: {value[:140]!r}")
    assert not hits, "Current person-facing text still uses the retired PM name:\n" + "\n".join(hits)


def test_person_guidance_routes_inspection_and_stop_through_pm():
    root = Path(cli.__file__).resolve().parents[2]
    # Developer diagnostics remain available, but the person's entry guide and
    # PM skill must not send a person to a second orchestrator terminal.
    paths = [root / "README.md", root / "skills/fa/SKILL.md"]
    guidance = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "fa orch watch" not in guidance, "Inspection should be explained by the PM, not a second terminal"
    assert "fa stop" in guidance and "PM" in guidance, "Explain the single emergency entrance and the PM"
