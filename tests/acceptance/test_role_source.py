"""Acceptance test for role-source. Locked by Foragent; do not edit."""

from copy import deepcopy
from datetime import UTC, datetime
import json

import pytest
import yaml

from test_init import setup
from test_init_role_agents import init_project
from test_orch_architect import WHY, architect_case, finish_session
from test_orch_inbox import inbox_command
from test_order_progress import offline_local_cli
from test_role_agents import frontmatter
from test_skills_autosync import cli_exit, isolated_home


DEFAULTS = ("orchestrator", "architect", "docs")
NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def role_entry(name, *, signals=()):
    return {"agent": f"fa-{name}", "skills": [f"fa-{name}"],
            "memory": f".claude/agent-memory/fa-{name}/", "signals": deepcopy(list(signals))}


def read_roles(root):
    config = yaml.safe_load((root / ".foragent.yml").read_text(encoding="utf-8"))
    roles = config.get("roles")
    assert isinstance(roles, dict), "fa init must write the roles mapping in .foragent.yml"
    return roles


def write_role_snapshot(root, roles):
    """Consumer fixtures, not an alternative role writer in the product.

    Like the existing phase-event fixtures, seed the documented source and its
    projections so consumers can be tested independently of fa init / fa role.
    """
    path = root / ".foragent.yml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["roles"] = deepcopy(roles)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    for name, entry in roles.items():
        agent = root / ".claude/agents" / f"{entry['agent']}.md"
        agent.parent.mkdir(parents=True, exist_ok=True)
        metadata = {"name": entry["agent"], "memory": "project", "skills": entry["skills"]}
        agent.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False)
                         + f"---\nFollow the {name} role skill.\n", encoding="utf-8")
        memory = root / entry["memory"] / "MEMORY.md"
        memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_text(f"# {name} project memory\n", encoding="utf-8")
        for skill in entry["skills"]:
            existing_skill(root, skill)


def existing_skill(root, name):
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Offline role skill\n---\n"
                    "Propose changes through fa orch request; the orchestrator decides.\n",
                    encoding="utf-8")
    (path.parent / "LICENSE").write_text("SPDX-License-Identifier: MIT\n", encoding="utf-8")


def tree_bytes(root):
    """Include directory existence, so refusing remove cannot silently remove empty memory dirs."""
    return {path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*") if ".git" not in path.relative_to(root).parts}


def assert_projection(root, name, entry):
    assert entry.get("agent") == f"fa-{name}", f"{name}: derive the role agent name from roles"
    assert isinstance(entry.get("skills"), list), f"{name}: roles.skills must be a list"
    assert entry.get("memory") == f".claude/agent-memory/fa-{name}/", f"{name}: use project role memory"
    signals = entry.get("signals")
    assert isinstance(signals, list), f"{name}: roles.signals must be a list, even when idle"
    for signal in signals:
        assert isinstance(signal, dict) and {"paths", "kinds"} <= signal.keys(), (
            f"{name}: each signal must declare paths and kinds")
        for field in ("paths", "kinds"):
            assert isinstance(signal[field], list) and all(isinstance(item, str) for item in signal[field]), (
                f"{name}: signal {field} must be a list of strings")
    metadata, _ = frontmatter(root / ".claude/agents" / f"{entry['agent']}.md")
    assert metadata.get("name") == entry["agent"] and metadata.get("memory") == "project"
    assert metadata.get("skills") == entry["skills"], f"{name}: agent skills must equal the roles source"
    assert (root / entry["memory"] / "MEMORY.md").is_file(), f"{name}: create the role's MEMORY.md"


def role_command(main, root, *args):
    return cli_exit(main, ["role", *args, "--repo-root", str(root)])


@pytest.mark.parametrize("name", DEFAULTS)
def test_init_creates_default_role_source_and_matching_projections(init_project, name):
    root, _, run = init_project
    assert not (root / ".foragent.yml").exists(), "Exercise fa init on a fresh temporary repository"
    run()
    roles = read_roles(root)
    assert set(DEFAULTS) <= roles.keys(), "Initialize orchestrator, architect and docs standing roles"
    assert f"fa-{name}" in roles[name].get("skills", []), f"{name}: attach its standing role skill"
    assert_projection(root, name, roles[name])


def test_role_add_set_remove_updates_source_and_derived_files(init_project, capsys):
    from foragent.cli import main

    root, _, run = init_project
    run()
    original = yaml.safe_load((root / ".foragent.yml").read_text(encoding="utf-8"))
    for name in ("audit-basics", "audit-deep"):
        existing_skill(root, name)
    capsys.readouterr()
    code = role_command(main, root, "add", "security", "--scope", "src/payment/**",
                        "--signal", "paths:src/payment/**", "--skill", "audit-basics",
                        "--why", "Confirmed in planning: payment code needs a standing security owner")
    output = capsys.readouterr()
    assert code == 0, f"fa role add must create a custom role: {output.err.strip()}"
    added = read_roles(root)
    assert "security" in added, "fa role add must persist the role in .foragent.yml"
    entry = added["security"]
    assert "audit-basics" in entry["skills"], "Use the person's chosen existing skill"
    scope = entry.get("scope", [])
    assert "src/payment/**" in ([scope] if isinstance(scope, str) else scope), "Persist the chosen role scope"
    assert any("src/payment/**" in signal["paths"] for signal in entry["signals"]), (
        "Persist the path signal supplied to fa role add")
    assert_projection(root, "security", entry)
    for name, value in original.get("roles", {}).items():
        assert added[name] == value, "Adding a custom role must preserve the other roles"
    memory = root / entry["memory"] / "MEMORY.md"
    memory.write_text("# Keep learned payment constraints\n", encoding="utf-8")
    assert role_command(main, root, "set", "security", "--skill", "audit-deep") == 0, (
        "fa role set must update the source and agent in one successful command")
    updated = read_roles(root)["security"]
    assert "audit-deep" in updated["skills"] and "audit-basics" not in updated["skills"]
    assert {key: value for key, value in updated.items() if key != "skills"} == {
        key: value for key, value in entry.items() if key != "skills"}, "Changing skills preserves the role's other settings"
    assert_projection(root, "security", updated)
    assert memory.read_text(encoding="utf-8") == "# Keep learned payment constraints\n"
    (memory.parent / "nested").mkdir()
    (memory.parent / "nested/lesson.md").write_text("Owned role memory\n", encoding="utf-8")
    assert role_command(main, root, "remove", "security") == 0, "fa role remove must remove a custom role"
    remaining = read_roles(root)
    assert "security" not in remaining, "Delete the role entry instead of disabling it"
    assert not (root / ".claude/agents/fa-security.md").exists(), "Delete the derived agent"
    assert not memory.parent.exists(), "Delete the entire role memory directory, including nested files"
    assert remaining == {name: value for name, value in added.items() if name != "security"}
    final = yaml.safe_load((root / ".foragent.yml").read_text(encoding="utf-8"))
    assert {key: value for key, value in final.items() if key != "roles"} == {
        key: value for key, value in original.items() if key != "roles"}, "Role commands only own roles in config"


@pytest.mark.parametrize("name", DEFAULTS)
def test_default_role_remove_refuses_without_changing_anything(init_project, name):
    from foragent.cli import main

    root, _, run = init_project
    run()
    assert name in read_roles(root), "The protected role must exist before attempting removal"
    before = tree_bytes(root)
    assert role_command(main, root, "remove", name) != 0, f"The default {name} role cannot be removed"
    assert tree_bytes(root) == before, "Refusing removal must leave config, agents and memory unchanged"


def test_doctor_names_a_role_whose_agent_skills_drifted(tmp_path, monkeypatch, capsys):
    case = offline_local_cli(tmp_path, monkeypatch)
    write_role_snapshot(case.root, {"security": role_entry("security")})
    agent = case.root / ".claude/agents/fa-security.md"
    agent.write_text(agent.read_text(encoding="utf-8").replace("- fa-security", "- unconfigured-skill"),
                     encoding="utf-8")
    before = tree_bytes(case.root)
    capsys.readouterr()
    code = cli_exit(case.main, ["doctor", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code != 0, "fa doctor must report role skill drift as a failing check"
    diagnostic = (output.out + output.err).casefold()
    assert "security" in diagnostic and "skill" in diagnostic, (
        "fa doctor must name security and its skill drift, not merely reject the command")
    assert tree_bytes(case.root) == before, "Doctor reports drift; the roles writer repairs it"
    assert case.gh_calls == []


def events(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.mark.parametrize("name", ("docs", "architect", "security"))
def test_generic_role_uses_worker_launch_guard_record_and_done_event(tmp_path, monkeypatch, capsys, name):
    case = architect_case(tmp_path, monkeypatch)
    config = case.root / ".foragent.yml"
    config.write_text(config.read_text(encoding="utf-8") + "executor: claude\n", encoding="utf-8")
    write_role_snapshot(case.root, {role: role_entry(role) for role in (*DEFAULTS, "security")})
    capsys.readouterr()
    code = inbox_command(case, "role", name, "--why", WHY)
    output = capsys.readouterr()
    assert code == 0, f"fa orch role {name} must launch the named role: {output.err.strip()}"
    assert len(case.launches) == 1 and case.turns == [], "Launch one independent session and return immediately"
    child, = case.launches
    assert child.args[:3] == case.worker_prefix, "Generic roles use the same launch path as workers and architect"
    assert child.kwargs.get("start_new_session") is True
    assert child.kwargs.get("cwd") == case.root
    assert child.kwargs.get("stdout") is not None and child.kwargs.get("stderr") is not None
    record_file = case.store.root / f"{name}.json"
    assert record_file.is_file(), f"Persist the session guard in {name}.json"
    record = json.loads(record_file.read_text(encoding="utf-8"))
    assert record["pid"] == child.pid and record["why"] == WHY
    assert datetime.fromisoformat(record["started_at"]).utcoffset() is not None
    calls = [row for row in events(case.store) if row["kind"] == f"{name}-call"]
    assert len(calls) == 1 and calls[0].get("why") == WHY, "Record the evidence once per role call"
    before = record_file.read_bytes()
    assert inbox_command(case, "role", name, "--why", "Another request") != 0
    assert len(case.launches) == 1 and record_file.read_bytes() == before, "Refuse a second live session of this role"
    finish_session(case, child)
    turn, = case.turns
    assert WHY in turn["prompt"] and turn.get("agent") == f"fa-{name}", "Load the role selected by the source"
    assert f".claude/agent-memory/fa-{name}/" in turn["prompt"], "Give the role its own project memory"
    done = [row for row in events(case.store) if row["kind"] == f"{name}-done"]
    assert len(done) == 1 and done[0]["exit_code"] == 0 and done[0]["started_at"] == record["started_at"]
    assert len([row for row in events(case.store) if row["kind"] == f"{name}-call"]) == 1
    assert inbox_command(case, "role", name, "--why", "Measure after the previous session ended") == 0
    assert len(case.launches) == 2, "Completion releases the per-role live-session guard"
    assert case.gh_calls == [] and case.pushes == [], "Roles report through the orchestrator, without contacting a person"


def test_architect_alias_shares_the_generic_architect_session(tmp_path, monkeypatch, capsys):
    case = architect_case(tmp_path, monkeypatch)
    write_role_snapshot(case.root, {name: role_entry(name) for name in DEFAULTS})
    assert inbox_command(case, "architect", "--why", WHY) == 0, "Keep fa orch architect as the architect instance"
    child, = case.launches
    capsys.readouterr()
    assert inbox_command(case, "role", "architect", "--why", WHY) != 0
    output = capsys.readouterr()
    assert "architect" in output.out + output.err and any(
        word in (output.out + output.err).casefold() for word in ("running", "alive", "active", "already")), (
        "The generic command must see the alias's live architect, not an unrelated guard")
    assert len(case.launches) == 1
    finish_session(case, child)
    assert inbox_command(case, "role", "architect", "--why", WHY) == 0, "The generic command works after alias completion"
    assert inbox_command(case, "architect", "--why", WHY) != 0, "The alias also sees a live generic architect"
    assert len(case.launches) == 2
    assert (case.store.root / "architect.json").is_file()
    assert len([row for row in events(case.store) if row["kind"] == "architect-done"]) == 1
    assert case.gh_calls == [] and case.pushes == []
