"""Acceptance test for skills-autosync. Locked by Foragent; do not edit."""

import json
from pathlib import Path

import pytest
import yaml


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for key, directory in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                           ("XDG_CACHE_HOME", ".cache")):
        monkeypatch.setenv(key, str(home / directory))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    # Prevent default remote skill libraries from leaking into offline cases.
    write_global_config({"skill_libraries": []})
    return home


def write_global_config(values):
    path = Path.home() / ".config/foragent/config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return path


def cli_exit(main, argv):
    """Treat argparse's nonzero SystemExit as the CLI's observable exit code."""
    try:
        return main(argv)
    except SystemExit as exc:
        return exc.code


def missing_library(git_history, tmp_path):
    git_history.update({
        "skills/demo/SKILL.md": "---\nname: demo\ndescription: An offline demo\n---\nDemo.\n",
        "LICENSE": "SPDX-License-Identifier: MIT\n",
    })
    destination = tmp_path / "libraries/demo-pack"
    config = write_global_config({"skill_libraries": [{
        "name": "demo-pack", "repo": str(git_history.origin), "path": str(destination),
    }]})
    return destination, config


def test_skills_autosync_catalog_clones_missing_library(git_history, tmp_path, capsys):
    from foragent.cli import main

    destination, _ = missing_library(git_history, tmp_path)
    assert not destination.exists()
    assert main(["skills", "catalog", "--json"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in catalog] == ["demo"]
    assert (destination / "skills/demo/SKILL.md").is_file()


def test_skills_autosync_install_clones_missing_library(git_history, tmp_path):
    from foragent.cli import main

    destination, _ = missing_library(git_history, tmp_path)
    assert main(["skills", "install", "demo", "--repo-root", str(git_history.clone)]) == 0
    assert destination.is_dir()
    assert (git_history.clone / ".claude/skills/demo/SKILL.md").read_bytes() == (
        destination / "skills/demo/SKILL.md").read_bytes()


def test_skills_autosync_clone_error_names_library_without_manual_sync(tmp_path, capsys):
    from foragent.cli import main

    write_global_config({"skill_libraries": [{
        "name": "unavailable-pack", "repo": str(tmp_path / "absent.git"),
        "path": str(tmp_path / "libraries/unavailable-pack"),
    }]})
    assert main(["skills", "catalog", "--json"]) != 0
    error = capsys.readouterr().err
    assert "unavailable-pack" in error
    assert "fa skills sync" not in error
