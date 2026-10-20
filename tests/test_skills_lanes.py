from dataclasses import replace
import json
from pathlib import Path

import pytest
import yaml

from foragent import cli
from foragent.config import Config, load_config
from foragent.git import GitError
from foragent.lanes import LaneError, add_lane
from foragent.skills import SkillError, SkillManager


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "libraries/knowledge-work-plugins"
    skill = root / "engineering/skills/testing-strategy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text('---\nname: testing-strategy\ndescription: |\n  Pick useful checks.\n---\nTesting instructions.\n')
    (skill / "references").mkdir()
    (skill / "references/example.md").write_text("Example\n")
    (root / "LICENSE").write_text("Apache License\nVersion 2.0\n")
    (root / "NOTICE").write_text("Library attribution\n")
    config = Config(skill_libraries=[{"name": "knowledge-work-plugins", "repo": "https://example.test/library",
                                      "path": str(root)}])
    commands = []

    def run(args):
        commands.append(args)
        return "a" * 40 + "\n" if args[-2:] == ["rev-parse", "HEAD"] else ""

    return root, config, SkillManager(config, run=run), commands


def test_catalog_metadata_and_multiple_library_prefixes(library, tmp_path):
    root, config, manager, _ = library
    assert manager.catalog() == [{"id": "engineering/testing-strategy", "name": "testing-strategy", "description": "Pick useful checks."}]
    second = tmp_path / "second"
    (second / "other/skills/testing-strategy").mkdir(parents=True)
    (second / "other/skills/testing-strategy/SKILL.md").write_text("---\nname: different\ndescription: Other skill\n---\n")
    manager = SkillManager(replace(config, skill_libraries=[*config.skill_libraries, {"name": "extra", "repo": "local", "path": str(second)}]))
    assert [skill["id"] for skill in manager.catalog()] == ["knowledge-work-plugins/engineering/testing-strategy", "extra/other/testing-strategy"]


def test_sync_clones_then_fast_forwards_with_no_live_network(tmp_path):
    directory = tmp_path / "libraries"
    commands = []
    manager = SkillManager(Config(), libraries_dir=directory, run=lambda args: commands.append(args) or "")
    assert manager.sync() == ["knowledge-work-plugins"]
    assert commands == [["git", "clone", "--", "https://github.com/anthropics/knowledge-work-plugins", str(directory / "knowledge-work-plugins")]]
    (directory / "knowledge-work-plugins").mkdir()
    manager.sync()
    assert commands[-1] == ["git", "-C", str(directory / "knowledge-work-plugins"), "pull", "--ff-only"]


def test_install_copies_full_skill_and_records_notice_once(library, tmp_path):
    source, _, manager, commands = library
    repo = tmp_path / "repo"
    repo.mkdir()
    assert manager.install("engineering/testing-strategy", repo) == "testing-strategy"
    destination = repo / ".claude/skills/testing-strategy"
    assert (destination / "SKILL.md").read_bytes() == (source / "engineering/skills/testing-strategy/SKILL.md").read_bytes()
    assert (destination / "references/example.md").read_text() == "Example\n"
    notice = (repo / "THIRD_PARTY_NOTICES.md").read_text()
    assert all(value in notice for value in ("https://example.test/library", "engineering/skills/testing-strategy", "a" * 40, "Apache-2.0 (LICENSE)", "Library attribution"))
    manager.install("knowledge-work-plugins/engineering/testing-strategy", repo)
    assert (repo / "THIRD_PARTY_NOTICES.md").read_text() == notice
    (destination / "SKILL.md").write_text("Custom local skill\n")
    with pytest.raises(SkillError, match="different existing skill"):
        manager.install("engineering/testing-strategy", repo)
    assert (destination / "SKILL.md").read_text() == "Custom local skill\n"
    assert (repo / "THIRD_PARTY_NOTICES.md").read_text() == notice


def test_spdx_license_is_used(library, tmp_path):
    source, _, manager, _ = library
    (source / "LICENSE").write_text("SPDX-License-Identifier: MIT\nMIT License\n")
    manager.install("engineering/testing-strategy", tmp_path)
    assert "License: MIT (LICENSE)" in (tmp_path / "THIRD_PARTY_NOTICES.md").read_text()


def test_conflicting_skill_or_missing_id_does_not_write(library, tmp_path):
    _, _, manager, _ = library
    with pytest.raises(SkillError, match="unknown skill"):
        manager.install("no/such-skill", tmp_path)
    destination = tmp_path / ".claude/skills/testing-strategy"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("different")
    with pytest.raises(SkillError, match="overwrite"):
        manager.install("engineering/testing-strategy", tmp_path)
    assert not (tmp_path / "THIRD_PARTY_NOTICES.md").exists()


def test_add_lane_agent_settings_and_scoped_commit(library, tmp_path):
    _, config, manager, _ = library
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".foragent.yml").write_text(yaml.safe_dump({"language": "ko", "skill_libraries": config.skill_libraries}))
    commits = []
    tracked, pushes = [], []

    class Git:
        def current_branch(self):
            return "main"

        def ensure_tracked(self, paths):
            tracked.append(paths)

        def commit_paths(self, paths, title):
            commits.append((paths, title))

        def push_base(self, branch):
            assert commits
            pushes.append(branch)

    lane = add_lane(root, "api", ["api/**", "shared/**"], skills=["engineering/testing-strategy"],
                    executor="claude", model="lane-model", manager=manager, git=Git())
    assert lane == {"paths": ["api/**", "shared/**"], "agent": "fa-api", "skills": ["testing-strategy"],
                    "executor": "claude", "model": "lane-model"}
    assert load_config(root).language == "ko" and load_config(root).lanes["api"] == lane
    text = (root / ".claude/agents/fa-api.md").read_text()
    header = yaml.safe_load(text.split("---")[1])
    assert header == {"name": "fa-api", "description": 'Foragent lane "api": implements issues in api/**, shared/**.',
                      "memory": "project", "skills": ["testing-strategy"]}
    assert "repository's api lane (api/**, shared/**)." in text
    assert "Record durable, project-specific lessons" in text
    expected = [".foragent.yml", ".claude/agents/fa-api.md", ".claude/skills/testing-strategy/SKILL.md",
                ".claude/skills/testing-strategy/references/example.md", "THIRD_PARTY_NOTICES.md"]
    assert commits == [(expected, "Foragent lane api")]
    assert tracked == [expected] and pushes == ["main"]
    before = (root / ".foragent.yml").read_text()
    with pytest.raises(LaneError, match="already exists"):
        add_lane(root, "api", ["different/**"], git=Git())
    assert (root / ".foragent.yml").read_text() == before and len(commits) == 1


def test_lane_checks_branch_and_agent_before_writing(tmp_path):
    (tmp_path / ".foragent.yml").write_text("{}\n")

    class Git:
        def current_branch(self):
            return "feature"

    with pytest.raises(GitError, match="base branch"):
        add_lane(tmp_path, "api", ["api/**"], git=Git())
    assert not (tmp_path / ".claude").exists()
    (tmp_path / ".claude/agents").mkdir(parents=True)
    (tmp_path / ".claude/agents/fa-api.md").write_text("Mine\n")
    with pytest.raises(LaneError, match="agent already exists"):
        add_lane(tmp_path, "api", ["api/**"], git=Git())


def test_skills_cli_routes_offline(library, tmp_path, monkeypatch, capsys):
    _, _, manager, _ = library
    monkeypatch.setattr(cli, "SkillManager", lambda config: manager)
    assert cli.main(["skills", "catalog", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "engineering/testing-strategy"
    assert cli.main(["skills", "sync"]) == 0
    assert "Synced:" in capsys.readouterr().out
    assert cli.main(["skills", "install", "engineering/testing-strategy", "--repo-root", str(tmp_path)]) == 0
    assert "Installed: testing-strategy" in capsys.readouterr().out
