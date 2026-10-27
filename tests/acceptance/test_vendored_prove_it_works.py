"""Acceptance test for vendored-prove-it-works. Locked by Foragent; do not edit."""

from pathlib import Path
import re

import pytest
import yaml

from test_doctor_live import invoke_doctor, live_doctor  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]
LANES = ("adopt", "order", "bench", "orch", "view", "board", "skills", "design")
PRINCIPLES = ("principle-prove-it-works", "principle-fix-root-causes")


def normalized(text):
    return " ".join(text.casefold().replace("`", "").replace("*", "").replace("’", "'").split())


def frontmatter(path):
    assert path.is_file(), f"Missing skill or agent file: {path.relative_to(ROOT)}"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", f"{path.name}: YAML frontmatter is required"
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    assert end is not None, f"{path.name}: close the YAML frontmatter"
    metadata = yaml.safe_load("\n".join(lines[1:end]))
    assert isinstance(metadata, dict), f"{path.name}: frontmatter must be a mapping"
    return metadata, "\n".join(lines[end + 1:])


def assert_lane_skills(skill, names):
    lanes = yaml.safe_load((ROOT / ".foragent.yml").read_text(encoding="utf-8"))["lanes"]
    assert set(names) <= set(lanes), f"Missing expected lanes: {sorted(set(names) - set(lanes))}"
    missing = []
    for lane in names:
        configured = lanes[lane].get("skills")
        assert isinstance(configured, list), f".foragent.yml:{lane}: skills must be a list"
        if skill not in configured:
            missing.append(f".foragent.yml:{lane}")
        agent = ROOT / f".claude/agents/fa-{lane}.md"
        metadata, _ = frontmatter(agent)
        declared = metadata.get("skills")
        assert isinstance(declared, list), f".claude/agents/fa-{lane}.md: skills must be a frontmatter list"
        if skill not in declared:
            missing.append(f".claude/agents/fa-{lane}.md")
    assert not missing, f"{skill} missing from skills lists: {', '.join(missing)}"


@pytest.mark.parametrize("name", PRINCIPLES)
def test_vendored_skill_preserves_pstack_mit_attribution(name):
    skill = ROOT / "skills" / name / "SKILL.md"
    metadata, body = frontmatter(skill)
    assert metadata.get("name") == name, "Use the vendored skill's canonical name"
    credits = "\n".join([skill.read_text(encoding="utf-8"), *(
        path.read_text(encoding="utf-8") for path in sorted(skill.parent.iterdir())
        if path.is_file() and path.name.casefold().startswith("license")
    )])
    text = normalized(credits)
    assert "pstack" in text, "Credit pstack as the upstream source"
    assert re.search(r"\bmit\b", text), "Retain pstack's MIT license attribution"
    assert re.search(r"\bcopyright\s+(?:\(c\)\s*|©\s*)?\d{4}\b[^.]*\blauren tan\b", text), (
        "Retain the upstream MIT copyright line naming Lauren Tan"
    )
    assert body.strip(), "The vendored SKILL.md must contain the skill, not just metadata"


@pytest.mark.parametrize("name", PRINCIPLES)
def test_every_lane_and_agent_loads_vendored_proof_skill(name):
    lanes = yaml.safe_load((ROOT / ".foragent.yml").read_text(encoding="utf-8"))["lanes"]
    assert set(LANES) <= set(lanes), "Keep all eight Foragent lanes"
    assert_lane_skills(name, tuple(lanes))


# D95, orchestrator, 2026-10-27: .foragent.yml is the single source of lane skills.
# src/foragent/lanes.py is its only writer: fa lanes add / fa lanes skills write
# each agent's frontmatter skills from the configuration in the same operation.
# Doctor reports drift without repairing it; the runner reads only configuration.
def test_every_lane_agent_skills_equal_the_configuration_source():
    lanes = yaml.safe_load((ROOT / ".foragent.yml").read_text(encoding="utf-8"))["lanes"]
    differences = []
    for lane, settings in lanes.items():
        agent = settings.get("agent") or f"fa-{lane}"
        metadata, _ = frontmatter(ROOT / f".claude/agents/{agent}.md")
        configured, declared = settings.get("skills"), metadata.get("skills")
        assert isinstance(configured, list), f".foragent.yml:{lane}: skills must be a list"
        assert isinstance(declared, list), f"{agent}: frontmatter skills must be a list"
        if configured != declared:
            differences.append(f"{lane} ({agent}): config={configured!r}, frontmatter={declared!r}")
    assert not differences, (
        "Lane agent skills must equal their .foragent.yml source: " + "; ".join(differences)
    )


def write_lane_skill_declarations(root, configured, declared):
    """Build independent declarations so the tests can represent a stale projection."""
    path = root / ".foragent.yml"
    config = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    config.update(skill_libraries=[], lanes={"api": {
        "paths": ["src/**"], "agent": "fa-shared", "skills": configured,
    }})
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    agent = root / ".claude/agents/fa-shared.md"
    agent.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"name": "fa-shared", "description": "Offline lane agent", "skills": declared}
    agent.write_text("---\n" + yaml.safe_dump(metadata) + "---\nImplement the lane.\n", encoding="utf-8")
    return path, agent


def test_doctor_reports_lane_skill_drift_and_is_quiet_when_matching(live_doctor, capsys):
    root = live_doctor.root
    skill = root / ".claude/skills/x/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: x\ndescription: Offline skill\n---\nUse focused checks.\n", encoding="utf-8")
    config, agent = write_lane_skill_declarations(root, ["x"], ["x"])
    before = config.read_bytes(), agent.read_bytes()
    matching_code, matching_report = invoke_doctor(live_doctor, capsys, "--repo-root", str(root))
    assert (config.read_bytes(), agent.read_bytes()) == before, "Doctor must leave matching declarations unchanged"
    assert matching_code == 0, f"Matching lane skills must pass doctor: {matching_report}"
    assert "api" not in normalized(matching_report), (
        f"Doctor must report no lane skill mismatch when declarations match: {matching_report}"
    )

    config, agent = write_lane_skill_declarations(root, ["x"], [])
    before = config.read_bytes(), agent.read_bytes()
    mismatch_code, mismatch_report = invoke_doctor(live_doctor, capsys, "--repo-root", str(root))
    assert (config.read_bytes(), agent.read_bytes()) == before, "Doctor reports drift; only the lanes writer repairs it"
    assert mismatch_code != 0, (
        f"Doctor must fail for lane api's mismatched skills: config=['x'], frontmatter=[]; got {mismatch_report}"
    )
    assert re.search(r"\bapi\b", normalized(mismatch_report)) and "skill" in normalized(mismatch_report), (
        f"Doctor must name lane api and its skills mismatch: {mismatch_report}"
    )
    assert live_doctor.gh == [], "Lane skill diagnosis must not contact GitHub"


def test_runner_empty_configured_skills_do_not_fall_back_to_agent(tmp_path):
    from foragent.config import load_config
    from foragent.run_lane_skills import lane_skill_names

    write_lane_skill_declarations(tmp_path, [], ["x"])
    names = lane_skill_names(load_config(tmp_path), "api", tmp_path, "fa-shared")
    assert names == [], (
        f"The runner must honor .foragent.yml skills: [] instead of the agent's stale [x]; got {names!r}"
    )
