"""Acceptance test for skills-add. Locked by Foragent; do not edit."""

import json

import yaml

from test_skills_autosync import cli_exit, isolated_home, write_global_config


def skill_pack(git_history, tmp_path):
    from conftest import git_command

    files = {f"{tool}/skills/polish/SKILL.md":
             "---\nname: polish\ndescription: Polish the writing\n---\nPolish.\n"
             for tool in (".claude", ".codex", ".cursor")}
    files.update({
        "skills/tone/SKILL.md": "---\nname: tone\ndescription: Check the tone\n---\nCheck.\n",
        "skills/tone/scripts/check.sh": "#!/bin/sh\nexit 0\n",
        "LICENSE": "SPDX-License-Identifier: MIT\n",
    })
    git_history.update(files)
    source = tmp_path / "skill-pack"
    git_command(tmp_path, "clone", str(git_history.origin), str(source))
    return source


def test_skills_add_previews_copies_license_and_scripts_without_registration(
    git_history, tmp_path, capsys, isolated_home,
):
    from foragent.cli import main

    source = skill_pack(git_history, tmp_path)
    config = write_global_config({"skill_libraries": []})
    before = config.read_bytes()
    libraries = isolated_home / ".local/share/foragent/libraries"
    assert not libraries.exists()
    assert main(["skills", "add", str(source), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == str(source)
    assert len(payload["skills"]) == 2
    skills = {item["name"]: item for item in payload["skills"]}
    assert set(skills) == {"polish", "tone"}
    for item in skills.values():
        assert {"name", "description", "license", "runs_scripts", "copies"} <= item.keys()
        assert item["license"] == "MIT" and item["description"]
    assert set(skills["polish"]["copies"]) == {
        f"{tool}/skills/polish" for tool in (".claude", ".codex", ".cursor")}
    assert skills["polish"]["runs_scripts"] is False
    assert skills["tone"]["runs_scripts"] is True
    assert skills["tone"]["copies"] == ["skills/tone"]
    assert config.read_bytes() == before
    assert not libraries.exists()


def test_skills_add_approval_registers_roles_and_catalog_merges_tool_copies(
    git_history, tmp_path, capsys,
):
    from foragent.cli import main

    source = skill_pack(git_history, tmp_path)
    config = write_global_config({"skill_libraries": []})
    assert main(["skills", "add", str(source), "--approve", "--role", "polish=style",
                 "--role", "tone=tool"]) == 0
    saved = yaml.safe_load(config.read_text())
    assert any(entry["repo"] == str(source) for entry in saved["skill_libraries"])
    capsys.readouterr()
    assert main(["skills", "catalog", "--json"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert len(catalog) == 2
    polish, = [item for item in catalog if item["name"] == "polish"]
    tone, = [item for item in catalog if item["name"] == "tone"]
    assert polish["role"] == "style"
    assert saved["skill_roles"][polish["id"]] == "style"
    assert saved["skill_roles"][tone["id"]] == "tool"


def test_skills_add_invalid_role_leaves_config_unchanged(git_history, tmp_path, capsys):
    from foragent.cli import main

    source = skill_pack(git_history, tmp_path)
    config = write_global_config({"skill_libraries": []})
    before = config.read_bytes()
    assert cli_exit(main, ["skills", "add", str(source), "--approve", "--role", "tone=pretty"]) != 0
    assert config.read_bytes() == before
    assert "pretty" in capsys.readouterr().err
