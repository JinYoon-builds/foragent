"""Acceptance test for ux-researcher-role (#1654). Locked by Foragent; do not edit.

fa init gives every project a default UX researcher role (agent file, project memory and a
roles entry whose signals say when it is called); `fa orch roles --json` lists it with
those call conditions; and Foragent ships the role, its skill with Foragent's own flows,
and a README note on what persona testing is and is not.
"""

import json
from pathlib import Path
import re

import yaml

from test_init_role_agents import init_project  # noqa: F401
from test_init import setup  # noqa: F401
from test_role_agents import assert_role_agent, frontmatter, normalized
from test_role_source import assert_projection, read_roles
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SKILL = "fa-ux-researcher"


def ux_role(roles):
    found = [(name, entry) for name, entry in roles.items() if SKILL in (entry.get("skills") or [])]
    assert len(found) == 1, f"Declare exactly one role that carries the {SKILL} skill"
    return found[0]


def test_init_creates_ux_researcher_role_listed_with_order_end_and_daily_calls(init_project, capsys):
    from foragent.cli import main

    root, _, run = init_project
    assert not (root / ".foragent.yml").exists(), "Start with a fresh project"
    run()
    name, entry = ux_role(read_roles(root))
    assert_projection(root, name, entry)
    assert entry["signals"], "The UX researcher must declare when it is called"
    config = root / ".foragent.yml"
    if "repo" not in (yaml.safe_load(config.read_text(encoding="utf-8")) or {}):
        # The offline setup has no origin remote to name the project's state by.
        config.write_text(config.read_text(encoding="utf-8") + "repo: offline/project\n", encoding="utf-8")
    capsys.readouterr()
    code = cli_exit(main, ["orch", "roles", "--json", "--repo-root", str(root)])
    output = capsys.readouterr()
    assert code == 0, f"fa orch roles --json must succeed after init: {output.err.strip()}"
    row = json.loads(output.out)["roles"].get(name)
    assert isinstance(row, dict) and row.get("default") is True, "List the UX researcher as a default role"
    conditions = json.dumps(row.get("signals"), ensure_ascii=False).casefold()
    assert "order" in conditions, "Its listed call conditions must include the end of an order"
    assert re.search(r"dai?ly|\bday\b|24 ?h", conditions), "Its listed call conditions must include a daily call"


def skill_text():
    folder = ROOT / "skills" / SKILL
    assert (folder / "SKILL.md").is_file(), f"Ship skills/{SKILL}/SKILL.md"
    metadata, _ = frontmatter(folder / "SKILL.md")
    assert metadata.get("name") == SKILL, f"Name the skill {SKILL}"
    return normalized(" ".join(path.read_text(encoding="utf-8") for path in sorted(folder.rglob("*.md"))))


def test_foragent_ships_ux_researcher_role_skill_flows_and_readme_note():
    config = yaml.safe_load((ROOT / ".foragent.yml").read_text(encoding="utf-8"))
    name, entry = ux_role(config.get("roles") or {})
    assert entry.get("signals"), "Foragent's own UX researcher must declare when it is called"
    assert_role_agent(ROOT, entry.get("agent") or f"fa-{name}")
    memory = (entry.get("memory") or f".claude/agent-memory/fa-{name}/").rstrip("/") + "/MEMORY.md"
    assert (ROOT / memory).is_file(), f"Ship the UX researcher's project memory: {memory}"

    text = skill_text()
    duties = (
        (r"persona", "play target personas"),
        (r"readme", "start from the README"),
        (r"(?:not|never|no)\b.{0,80}(?:source|code)", "stay out of source code"),
        (r"(?:second|another|separate) (?:session|pass).{0,160}confirm|confirm.{0,160}(?:second|another|separate) (?:session|pass)",
         "have a second session confirm each find"),
        (r"orchestrat", "deliver finds through the orchestrator"),
        (r"memory.{0,200}flows?.{0,120}(?:break|broke|fail)|(?:break|broke|fail)\w*.{0,120}flows?.{0,200}memory",
         "keep only which flows break in memory"),
    )
    for pattern, duty in duties:
        assert re.search(pattern, text), f"{SKILL} must {duty}"
    flows = (
        (r"reinstall.{0,240}dashboard|dashboard.{0,240}reinstall", "the dashboard follows a reinstall"),
        (r"orca.{0,240}(?:phone|narrow|mobile)|(?:phone|narrow|mobile).{0,240}orca", "the dashboard in a phone-width Orca pane"),
        (r"merge.owner.{0,300}question|question.{0,300}merge.owner", "a merge-owner-queued PR shows the waiting word, not 'question'"),
        (r"/fa\b.{0,240}dashboard|dashboard.{0,240}/fa\b", "/fa opens the PM and the dashboard"),
    )
    for pattern, flow in flows:
        assert re.search(pattern, text), f"{SKILL} must list Foragent's flow: {flow}"

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    english, _, korean = readme.partition("## 한국어")
    english = normalized(english)
    assert "ux researcher" in english, "The README must name the UX researcher role"
    assert re.search(r"persona", english), "The README must say the UX researcher tests by playing personas"
    assert re.search(r"(?:not|never|no)\b.{0,120}(?:replace|substitute).{0,120}real users|real users.{0,160}(?:not|never)",
                     english), "The README must say it does not replace research with real users"
    assert "UX 리서처" in korean, "The Korean README must name the role UX 리서처"
