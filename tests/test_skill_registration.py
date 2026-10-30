"""Offline registration, approval, and copy resolution beyond the acceptance contract."""

import json
from pathlib import Path
import subprocess

import pytest
import yaml

from foragent import cli
from foragent.config import Config, load_global_config
from foragent.skills import SkillError, SkillManager


@pytest.fixture
def pack(git_history, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    marker = tmp_path / "script-ran"
    git_history.update({
        **{f"{tool}/skills/polish/SKILL.md":
           "---\nname: polish\ndescription: Polish the writing\n---\nPolish.\n"
           for tool in (".claude", ".codex", ".cursor", ".another-tool")},
        "skills/polish/SKILL.md": "---\nname: polish\ndescription: Shared writing skill\n---\nPolish.\n",
        ".cursor/skills/polish/scripts/check": f"#!/bin/sh\ntouch '{marker}'\n",
        "engineering/skills/testing/SKILL.md": "---\nname: testing\ndescription: Review tests\n---\nReview.\n",
        "LICENSE": "SPDX-License-Identifier: MIT\n",
    })
    path = home / ".config/foragent/config.yml"
    return str(git_history.origin), path, marker


def write_config(path, **values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"skill_libraries": [], **values}))


def test_preview_without_config_uses_only_a_disposable_clone(pack):
    source, config, marker = pack
    commands = []

    def run(args):
        commands.append(args)
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout

    manager = SkillManager(run=run)
    preview = manager.add(source)
    assert len(preview["skills"]) == 2
    assert preview["approved"] is False
    assert len(commands) == 1 and commands[0][0:3] == ["git", "clone", "--"]
    assert not Path(commands[0][-1]).exists()
    assert not manager.libraries_dir.exists()
    assert not config.exists() and not marker.exists()
    polish = next(skill for skill in preview["skills"] if skill["name"] == "polish")
    assert polish["id"] == "origin:polish" and len(polish["copies"]) == 5
    assert polish["description"] == "Shared writing skill"
    assert polish["runs_scripts"] is True and polish["role"] is None


@pytest.mark.parametrize("roles, message", [
    ([], "confirm a role for each skill"),
    (["polish=style"], "origin:engineering/testing"),
    (["polish=style", "missing=tool"], "unknown skill: missing"),
    (["polish=pretty"], "pretty"),
    (["polish"], "expected SKILL="),
    (["=tool"], "expected SKILL="),
    (["polish=style", "origin:polish=review", "testing=tool"], "conflicting roles"),
])
def test_invalid_approval_preserves_config_and_cache(pack, roles, message, capsys):
    source, path, marker = pack
    write_config(path, language="ko")
    before = path.read_bytes()
    argv = ["skills", "add", source, "--approve"]
    for role in roles:
        argv.extend(["--role", role])
    assert cli.main(argv) == 1
    assert message in capsys.readouterr().err
    assert path.read_bytes() == before
    assert not SkillManager().libraries_dir.exists() and not marker.exists()


def test_roles_need_approval_and_missing_source_leaves_no_registration(pack, tmp_path, capsys):
    source, path, _ = pack
    assert cli.main(["skills", "add", source, "--role", "polish=style"]) == 1
    assert "--approve" in capsys.readouterr().err
    assert cli.main(["skills", "add", str(tmp_path / "missing.git"), "--approve"]) == 1
    assert "could not clone library missing" in capsys.readouterr().err
    assert not path.exists() and not SkillManager().libraries_dir.exists()


def test_approval_is_idempotent_and_preserves_settings_and_other_roles(pack, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # The catalog adds the project's vendored library; count only this pack.
    source, path, marker = pack
    write_config(path, language="ko", tiers={"s": {"claude_model": "small"}},
                 skill_roles={"other:existing": "tool"})
    manager = SkillManager()
    roles = ["polish=domain", "origin:engineering/testing=review"]
    manager.add(source, approve=True, roles=roles)
    manager.add(source, approve=True, roles=roles)
    saved = yaml.safe_load(path.read_text())
    assert saved["skill_libraries"] == [{"name": "origin", "repo": source}]
    assert saved["skill_roles"] == {"other:existing": "tool", "origin:polish": "domain",
                                    "origin:engineering/testing": "review"}
    assert saved["language"] == "ko" and saved["tiers"] == {"s": {"claude_model": "small"}}
    assert not marker.exists()
    assert cli.main(["skills", "catalog"]) == 0
    output = capsys.readouterr().out
    assert "Role\tRuns scripts\tCopies" in output and "domain\tyes" in output
    assert cli.main(["skills", "catalog", "--json"]) == 0
    assert len([row for row in json.loads(capsys.readouterr().out) if not row["id"].startswith("pstack:")]) == 2
    # Every selector resolves the same shared copy, including previously exposed tool IDs.
    for alias in ("polish", "origin:polish", "origin:.codex/polish"):
        selected = manager._select(alias)
        assert selected.path == manager.libraries_dir / "origin/skills/polish"
        assert selected.role == "domain"


def test_approval_keeps_configured_library_name_and_custom_cache(pack, tmp_path):
    source, path, _ = pack
    cache = tmp_path / "custom-cache"
    write_config(path, skill_libraries=[{"name": "renamed", "repo": source, "path": str(cache)}])
    manager = SkillManager()
    manager.add(source, approve=True, roles=["polish=style", "testing=review"])
    saved = load_global_config()
    assert len(saved.skill_libraries) == 1
    assert saved.skill_libraries[0]["path"] == str(cache)
    assert saved.skill_roles == {"renamed:polish": "style", "renamed:engineering/testing": "review"}
    assert len(manager.catalog()) == 2 and cache.is_dir()


def test_duplicate_library_name_is_rejected_before_cloning(pack):
    source, path, _ = pack
    write_config(path, skill_libraries=[{"name": "origin", "repo": "https://example.test/other"}])
    before = path.read_bytes()
    with pytest.raises(SkillError, match="duplicate library name"):
        SkillManager().add(source, approve=True, roles=["polish=style", "testing=review"])
    assert path.read_bytes() == before


def test_ambiguous_role_name_requires_qualified_id(pack, git_history):
    source, path, _ = pack
    git_history.update({"other/skills/polish/SKILL.md":
                        "---\nname: polish\ndescription: A separate plugin skill\n---\n"})
    with pytest.raises(SkillError, match="ambiguous skill: polish"):
        SkillManager().add(source, approve=True, roles=["polish=style"])
    assert not path.exists()
    result = SkillManager().add(source, approve=True, roles=["origin:polish=style", "testing=review",
                                                           "origin:other/polish=tool"])
    assert {skill["id"]: skill["role"] for skill in result["skills"]} == {
        "origin:polish": "style", "origin:engineering/testing": "review", "origin:other/polish": "tool",
    }


def test_empty_and_malformed_packs_leave_config_unchanged(git_history, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(SkillError, match="no skills found"):
        SkillManager(Config(skill_libraries=[])).add(str(git_history.origin), approve=True)
    git_history.update({"skills/broken/SKILL.md": "---\nname: broken\n---\n"})
    with pytest.raises(SkillError, match="requires name and description"):
        SkillManager(Config(skill_libraries=[])).add(str(git_history.origin), approve=True)
    assert not (tmp_path / "home").exists()
