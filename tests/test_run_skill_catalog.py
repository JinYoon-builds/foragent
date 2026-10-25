"""Per-issue skill catalog and the implementer's `Skills used:` report."""

import json
from pathlib import Path

import pytest

from foragent.config import Config
from foragent.run_review import parse_code_review
from foragent.run_skill_catalog import (
    available_skills, catalog_section, parse_skills_used, report_line,
)
from foragent.state import Run


def library(tmp_path: Path, roles: dict[str, str | None]) -> Config:
    root = tmp_path / "library"
    for name in roles:
        skill = root / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: {name}\ndescription: About {name}\n  spread over lines\n---\nBody\n")
    return Config(skill_libraries=[{"name": "local", "repo": "unused", "path": str(root)}],
                  skill_roles={f"local:{name}": role for name, role in roles.items() if role is not None})


def test_catalog_offers_only_approved_nonstyle_skills_that_are_not_fixed(tmp_path):
    config = library(tmp_path, {"tool-a": "tool", "rev": "review", "dom": "domain",
                                "style-a": "style", "unclassified": None, "fixed-tool": "tool"})
    offered = available_skills(config, ["fixed-tool"])
    assert [name for name, _, _ in offered] == ["dom", "rev", "tool-a"]
    name, description, path = offered[0]
    assert description == "About dom spread over lines"
    assert path == str((tmp_path / "library/skills/dom/SKILL.md").resolve())


def test_catalog_skips_missing_libraries_without_cloning(tmp_path):
    config = Config(skill_libraries=[{"name": "gone", "repo": "https://example.invalid/x.git",
                                      "path": str(tmp_path / "missing")}])
    assert available_skills(config, []) == []


def test_catalog_section_always_asks_for_the_report():
    empty = catalog_section([])
    assert "## Available skills" in empty and "Skills used: none" in empty
    assert "Skills used: <names>" in empty
    text = catalog_section([("tone", "Check tone", "/x/SKILL.md")])
    assert "- tone: Check tone (/x/SKILL.md)" in text and "Skills used: <names>" in text


@pytest.mark.parametrize("message,expected", [
    ("Done.\nSkills used: tone, polish-x", ["tone", "polish-x"]),
    ("Skills used: none", []),
    ("Skills used: None.", []),
    ("**Skills used:** `tone`, `tone`", ["tone"]),
    ("- Skills used: a\nmore text\nSkills used: b", ["b"]),
    ("Implemented feature.", None),
    ("", None),
])
def test_parse_skills_used(message, expected):
    assert parse_skills_used(message) == expected


def test_report_line_distinguishes_none_from_not_reported():
    assert report_line(["a", "b"], True) == "Skills used: a, b"
    assert report_line([], True) == "Skills used: none"
    assert report_line([], False) == "Skills used: not reported"


def test_run_records_default_to_no_skill_report():
    run = Run(1, "b", "w", None, "t", "t")
    assert (run.skills_reported, run.skills_used) == (False, [])


@pytest.mark.parametrize("message,verdict", [
    ("VERDICT: PASS", "PASS"), ("\nVERDICT: FAIL\nx.py:1 — broken", "FAIL"), ("garbage", "UNUSABLE"),
    ("Error: request failed\nVERDICT: PASS", "UNUSABLE"),
])
def test_claude_review_reads_a_plain_verdict_line_when_output_is_not_json(message, verdict):
    assert parse_code_review(message).verdict == verdict


def test_claude_review_json_events_keep_their_rules():
    assert parse_code_review(json.dumps({"result": "VERDICT: PASS"})).verdict == "UNUSABLE"
