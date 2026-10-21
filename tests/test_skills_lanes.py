from dataclasses import replace
import json
from pathlib import Path

import pytest
import yaml

from foragent import cli
from foragent.config import Config, load_config
from foragent.git import GitError
from foragent.lanes import LaneError, add_lane, validate_lane
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


@pytest.fixture
def two_libraries(library, tmp_path):
    kwp, config, _, commands = library
    skills = tmp_path / "libraries/skills"
    frontend = skills / "skills/frontend-design"
    frontend.mkdir(parents=True)
    (frontend / "SKILL.md").write_text("---\nname: frontend-design\ndescription: Choose a design direction.\n---\nDesign body.\n")
    (frontend / "LICENSE.txt").write_text("SPDX-License-Identifier: MIT\nFrontend copyright\n")
    docx = skills / "skills/docx"
    docx.mkdir()
    (docx / "SKILL.md").write_text("---\nname: docx\ndescription: Create documents.\n---\nDocument body.\n")
    (docx / "LICENSE.txt").write_text("Source-available. Governed by commercial terms.\n")
    (skills / "LICENSE").write_text("Apache License\nVersion 2.0\nLibrary root copyright\n")
    config = replace(config, skill_libraries=[*config.skill_libraries, {
        "name": "skills", "repo": "https://github.com/anthropics/skills", "path": str(skills),
    }])
    manager = SkillManager(config, run=lambda args: commands.append(args) or "a" * 40 + "\n")
    return kwp, skills, config, manager, commands


def test_catalog_metadata_and_multiple_library_prefixes(library, tmp_path):
    root, config, manager, _ = library
    assert manager.catalog() == [{"id": "kwp:engineering/testing-strategy", "name": "testing-strategy",
                                  "description": "Pick useful checks.", "license": "Apache-2.0"}]
    second = tmp_path / "second"
    (second / "other/skills/testing-strategy").mkdir(parents=True)
    (second / "other/skills/testing-strategy/SKILL.md").write_text("---\nname: different\ndescription: Other skill\n---\n")
    manager = SkillManager(replace(config, skill_libraries=[*config.skill_libraries, {"name": "extra", "repo": "local", "path": str(second)}]))
    assert [skill["id"] for skill in manager.catalog()] == ["kwp:engineering/testing-strategy", "extra:other/testing-strategy"]


def test_two_library_layouts_have_stable_ids_and_per_skill_licenses(two_libraries, tmp_path):
    kwp, skills, _, manager, _ = two_libraries
    assert {row["id"]: row["license"] for row in manager.catalog()} == {
        "kwp:engineering/testing-strategy": "Apache-2.0",
        "skills:docx": "source-available", "skills:frontend-design": "MIT",
    }
    repo = tmp_path / "repo"
    assert manager.install("kwp:engineering/testing-strategy", repo) == "testing-strategy"
    assert manager.install("skills:frontend-design", repo) == "frontend-design"
    assert (repo / ".claude/skills/frontend-design/SKILL.md").read_bytes() == (skills / "skills/frontend-design/SKILL.md").read_bytes()
    assert (repo / ".claude/skills/testing-strategy/SKILL.md").read_bytes() == (kwp / "engineering/skills/testing-strategy/SKILL.md").read_bytes()
    notices = (repo / "THIRD_PARTY_NOTICES.md").read_text()
    assert "License: Apache-2.0 (LICENSE)" in notices and "License: MIT (LICENSE.txt)" in notices
    assert "Frontend copyright" in notices and "Library root copyright" not in notices


@pytest.mark.parametrize("identity", [
    "engineering/testing-strategy", "kwp:engineering/testing-strategy",
    "knowledge-work-plugins/engineering/testing-strategy", "knowledge-work-plugins:engineering/testing-strategy",
    "frontend-design", "skills:frontend-design", "skills/frontend-design",
])
def test_unique_legacy_ids_and_new_prefixed_ids_are_accepted(two_libraries, tmp_path, identity):
    manager = two_libraries[3]
    installed = manager.install(identity, tmp_path / "repo")
    assert installed == ("frontend-design" if "frontend-design" in identity else "testing-strategy")


def test_ambiguous_unprefixed_id_is_rejected_with_choices(two_libraries, tmp_path):
    _, skills, _, manager, commands = two_libraries
    duplicate = skills / "engineering/skills/testing-strategy"
    duplicate.mkdir(parents=True)
    (duplicate / "SKILL.md").write_text("---\nname: testing-strategy\ndescription: Another strategy.\n---\nOther body.\n")
    with pytest.raises(SkillError, match="ambiguous skill") as error:
        manager.install("engineering/testing-strategy", tmp_path / "repo")
    assert "kwp:engineering/testing-strategy" in str(error.value)
    assert "skills:engineering/testing-strategy" in str(error.value)
    assert commands == [] and not (tmp_path / "repo").exists()
    assert manager.install("skills:engineering/testing-strategy", tmp_path / "repo") == "testing-strategy"


def test_sync_clones_then_fast_forwards_with_no_live_network(tmp_path):
    directory = tmp_path / "libraries"
    commands = []
    manager = SkillManager(Config(), libraries_dir=directory, run=lambda args: commands.append(args) or "")
    assert manager.sync() == ["knowledge-work-plugins", "skills"]
    assert commands == [
        ["git", "clone", "--", "https://github.com/anthropics/knowledge-work-plugins", str(directory / "knowledge-work-plugins")],
        ["git", "clone", "--", "https://github.com/anthropics/skills", str(directory / "skills")],
    ]
    (directory / "knowledge-work-plugins").mkdir()
    (directory / "skills").mkdir()
    manager.sync()
    assert commands[-2:] == [
        ["git", "-C", str(directory / "knowledge-work-plugins"), "pull", "--ff-only"],
        ["git", "-C", str(directory / "skills"), "pull", "--ff-only"],
    ]


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


@pytest.mark.parametrize("filename,text,license_id", [
    ("LICENSE", "SPDX-License-Identifier: Apache-2.0\n", "Apache-2.0"),
    ("LICENSE.txt", "SPDX-License-Identifier: MIT\n", "MIT"),
    ("LICENSE.txt", "SPDX-License-Identifier: apache-2.0\n", "Apache-2.0"),
    ("LICENSE.txt", "BSD 2-Clause License\n", "BSD-2-Clause"),
    ("LICENSE", "BSD 3-Clause License\n", "BSD-3-Clause"),
    ("LICENSE", "SPDX-License-Identifier: BSD-4-Clause\n", "BSD-4-Clause"),
    ("LICENSE.txt", "SPDX-License-Identifier: 0BSD\n", "0BSD"),
    ("LICENSE", "ISC License\n", "ISC"),
    ("LICENSE.txt", "Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted.\n", "ISC"),
])
def test_skill_license_is_used_before_library_root(library, tmp_path, filename, text, license_id):
    source, _, manager, _ = library
    (source / "engineering/skills/testing-strategy" / filename).write_text(text)
    assert manager.catalog()[0]["license"] == license_id
    repo = tmp_path / "repo"
    manager.install("kwp:engineering/testing-strategy", repo)
    notices = (repo / "THIRD_PARTY_NOTICES.md").read_text()
    assert f"License: {license_id} ({filename})" in notices
    assert text.strip() in notices
    assert "Apache License\nVersion 2.0" not in notices


@pytest.mark.parametrize("text", [
    "Source-available. Use only under commercial terms.\n", "Proprietary license.\n",
    "Unknown terms. All rights reserved.\n", "", "SPDX-License-Identifier: LicenseRef-Custom\n",
    "SPDX-License-Identifier: MIT OR LicenseRef-Custom\n",
    "SPDX-License-Identifier: Apache-2.0 WITH Vendor-Restriction\n",
    "Apache License Version 2.0, modified for non-commercial use only.\n",
    "These terms are not the MIT License. Custom conditions apply.\n",
    "Example only: Apache License Version 2.0. Actual conditions are unspecified.\n",
    "BSD 3-Clause No Nuclear License\nRedistribution and use in source and binary forms\nNeither the name\n",
    b"\xff\xfeUnknown license encoding",
])
def test_nonfree_or_unknown_skill_overrides_open_source_root_and_writes_nothing(library, tmp_path, text):
    source, _, manager, commands = library
    license_file = source / "engineering/skills/testing-strategy/LICENSE.txt"
    license_file.write_bytes(text) if isinstance(text, bytes) else license_file.write_text(text)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "keep.txt").write_text("Keep me.\n")
    for action in (manager.check_install, manager.install):
        with pytest.raises(SkillError, match="cannot confirm an open-source license") as error:
            action("kwp:engineering/testing-strategy", repo)
        assert "LICENSE.txt" in str(error.value)
    assert list(repo.iterdir()) == [repo / "keep.txt"] and commands == []


def test_a_known_library_name_does_not_imply_an_open_source_license(tmp_path):
    root = tmp_path / "library"
    skill = root / "skills/example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: example\ndescription: Example.\n---\nBody.\n")
    manager = SkillManager(Config(skill_libraries=[{
        "name": "knowledge-work-plugins", "repo": "local", "path": str(root),
    }]), run=lambda args: pytest.fail("license refusal must precede Git operations"))
    assert manager.catalog()[0]["license"] == "unknown"
    with pytest.raises(SkillError, match="no LICENSE or LICENSE.txt"):
        manager.install("kwp:example", tmp_path / "repo")
    assert not (tmp_path / "repo").exists()


def test_skill_open_source_license_can_override_a_nonfree_root(library, tmp_path):
    source, _, manager, _ = library
    (source / "LICENSE").write_text("Source-available root terms.\n")
    (source / "engineering/skills/testing-strategy/LICENSE.txt").write_text("MIT License\n")
    manager.install("engineering/testing-strategy", tmp_path / "repo")
    assert manager.catalog()[0]["license"] == "MIT"


def test_license_file_priority_does_not_skip_restrictive_skill_terms(library, tmp_path):
    source, _, manager, _ = library
    skill = source / "engineering/skills/testing-strategy"
    (skill / "LICENSE").write_text("Source-available terms.\n")
    (skill / "LICENSE.txt").write_text("MIT License\n")
    with pytest.raises(SkillError, match="source-available"):
        manager.install("engineering/testing-strategy", tmp_path / "repo")


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


def test_validate_and_add_lane_accept_more_than_three_skills(library, tmp_path):
    source, _, manager, _ = library
    names = [f"skill-{number}" for number in range(6)]
    for name in names:
        path = source / "engineering/skills" / name
        path.mkdir()
        (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Useful skill.\n---\nInstructions.\n")
    identities = [f"kwp:engineering/{name}" for name in names]
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".foragent.yml").write_text("{}\n")
    assert validate_lane(root, Config(), "design", ["ui/**"], identities, manager=manager) == names
    assert not (root / ".claude").exists()
    commits = []

    class Git:
        def current_branch(self):
            return "main"

        def ensure_tracked(self, paths):
            pass

        def commit_paths(self, paths, title):
            commits.append(paths)

        def push_base(self, branch):
            pass

    assert add_lane(root, "design", ["ui/**"], skills=identities, manager=manager, git=Git())["skills"] == names
    assert load_config(root).lanes["design"]["skills"] == names
    assert all(f".claude/skills/{name}/SKILL.md" in commits[0] for name in names)


def test_nonfree_lane_skill_is_rejected_before_any_lane_files_are_written(two_libraries, tmp_path):
    manager = two_libraries[3]
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".foragent.yml").write_text("{}\n")
    with pytest.raises(SkillError, match="source-available"):
        add_lane(root, "design", ["ui/**"], skills=["skills:frontend-design", "skills:docx"], manager=manager)
    assert list(root.iterdir()) == [root / ".foragent.yml"]


def test_nonfree_install_cli_explains_refusal_and_catalog_still_lists_it(two_libraries, tmp_path, monkeypatch, capsys):
    manager = two_libraries[3]
    monkeypatch.setattr(cli, "SkillManager", lambda config: manager)
    assert cli.main(["skills", "catalog", "--json"]) == 0
    assert any(row["id"] == "skills:docx" and row["license"] == "source-available"
               for row in json.loads(capsys.readouterr().out))
    assert cli.main(["skills", "install", "skills:docx", "--repo-root", str(tmp_path / "repo")]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "source-available" in output.err and "skills:docx" in output.err
    assert not (tmp_path / "repo").exists()


def test_skills_cli_routes_offline(library, tmp_path, monkeypatch, capsys):
    _, _, manager, _ = library
    monkeypatch.setattr(cli, "SkillManager", lambda config: manager)
    assert cli.main(["skills", "catalog", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["id"] == "kwp:engineering/testing-strategy" and row["license"] == "Apache-2.0"
    assert cli.main(["skills", "catalog"]) == 0
    output = capsys.readouterr().out
    assert "ID\tName\tLicense\tDescription" in output and "Apache-2.0" in output
    assert cli.main(["skills", "sync"]) == 0
    assert "Synced:" in capsys.readouterr().out
    assert cli.main(["skills", "install", "engineering/testing-strategy", "--repo-root", str(tmp_path)]) == 0
    assert "Installed: testing-strategy" in capsys.readouterr().out
