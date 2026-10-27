from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from foragent import cli
from foragent.config import Config, load_config
from foragent.git import GitError, GitRepo
from foragent.lanes import LaneError, add_lane, set_lane_skills, validate_lane
from foragent.license_texts import STANDARD_LICENSES
from foragent.skills import SkillError, SkillManager, copy_lane_skills


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "libraries/knowledge-work-plugins"
    skill = root / "engineering/skills/testing-strategy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text('---\nname: testing-strategy\ndescription: |\n  Pick useful checks.\n---\nTesting instructions.\n')
    (skill / "references").mkdir()
    (skill / "references/example.md").write_text("Example\n")
    (root / "LICENSE").write_text("SPDX-License-Identifier: Apache-2.0\nApache License\nVersion 2.0\n")
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
    (skills / "LICENSE").write_text("SPDX-License-Identifier: Apache-2.0\nApache License\nVersion 2.0\nLibrary root copyright\n")
    config = replace(config, skill_libraries=[*config.skill_libraries, {
        "name": "skills", "repo": "https://github.com/anthropics/skills", "path": str(skills),
    }])
    manager = SkillManager(config, run=lambda args: commands.append(args) or "a" * 40 + "\n")
    return kwp, skills, config, manager, commands


def test_catalog_metadata_and_multiple_library_prefixes(library, tmp_path):
    root, config, manager, _ = library
    assert manager.catalog() == [{"id": "kwp:engineering/testing-strategy", "name": "testing-strategy",
                                  "description": "Pick useful checks.", "license": "Apache-2.0", "role": None,
                                  "runs_scripts": False, "copies": ["engineering/skills/testing-strategy"]}]
    second = tmp_path / "second"
    (second / "other/skills/testing-strategy").mkdir(parents=True)
    (second / "other/skills/testing-strategy/SKILL.md").write_text("---\nname: different\ndescription: Other skill\n---\n")
    manager = SkillManager(replace(config, skill_libraries=[*config.skill_libraries, {"name": "extra", "repo": "local", "path": str(second)}]))
    assert [skill["id"] for skill in manager.catalog()] == ["kwp:engineering/testing-strategy", "extra:other/testing-strategy"]


def test_two_library_layouts_have_stable_ids_and_per_skill_licenses(two_libraries, tmp_path):
    kwp, skills, _, manager, _ = two_libraries
    assert {row["id"]: row["license"] for row in manager.catalog()} == {
        "kwp:engineering/testing-strategy": "Apache-2.0",
        "skills:docx": "unknown", "skills:frontend-design": "MIT",
    }
    repo = tmp_path / "repo"
    assert manager.install("kwp:engineering/testing-strategy", repo, commit=True) == "testing-strategy"
    assert manager.install("skills:frontend-design", repo, commit=True) == "frontend-design"
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
    installed = manager.install(identity, tmp_path / "repo", commit=True)
    assert installed == ("frontend-design" if "frontend-design" in identity else "testing-strategy")


def test_ambiguous_unprefixed_id_is_rejected_with_choices(two_libraries, tmp_path):
    _, skills, _, manager, commands = two_libraries
    duplicate = skills / "engineering/skills/testing-strategy"
    duplicate.mkdir(parents=True)
    (duplicate / "SKILL.md").write_text("---\nname: testing-strategy\ndescription: Another strategy.\n---\nOther body.\n")
    with pytest.raises(SkillError, match="ambiguous skill") as error:
        manager.install("engineering/testing-strategy", tmp_path / "repo", commit=True)
    assert "kwp:engineering/testing-strategy" in str(error.value)
    assert "skills:engineering/testing-strategy" in str(error.value)
    assert commands == [] and not (tmp_path / "repo").exists()
    assert manager.install("skills:engineering/testing-strategy", tmp_path / "repo", commit=True) == "testing-strategy"


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


def test_catalog_and_lane_install_clone_once_without_updating_existing_library(library, git_history, tmp_path):
    _, config, _, _ = library
    git_history.update({"skills/demo/SKILL.md": "---\nname: demo\ndescription: Original\n---\n"})
    destination = tmp_path / "new-cache/demo-pack"
    config.skill_libraries.append({"name": "demo-pack", "repo": str(git_history.origin), "path": str(destination)})
    commands = []

    def run(args):
        commands.append(args)
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout

    manager = SkillManager(config, run=run)
    # The runner's cache-only lookup must remain offline.
    assert manager.cached_skill("demo") is None
    assert commands == []
    assert manager.catalog()[-1]["description"] == "Original"
    git_history.update({"skills/demo/SKILL.md": "---\nname: demo\ndescription: Updated\n---\n"})
    repo = tmp_path / "repo"
    assert validate_lane(repo, config, "demo", ["src/**"], ["demo"], None, None, manager) == ["demo"]
    assert manager.install("demo", repo) == "demo"
    assert manager.catalog()[-1]["description"] == "Original"
    assert commands == [["git", "clone", "--", str(git_history.origin), str(destination)]]


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(128, ["git", "clone"], stderr="remote unavailable"),
    subprocess.TimeoutExpired(["git", "clone"], 300),
    OSError("git unavailable"),
])
def test_autoclone_error_identifies_library(tmp_path, failure):
    def run(args):
        raise failure

    manager = SkillManager(Config(skill_libraries=[{"name": "demo-pack", "repo": "unavailable",
                                                   "path": str(tmp_path / "cache")}]), run=run)
    with pytest.raises(SkillError, match="could not clone library demo-pack") as error:
        manager.catalog()
    assert "fa skills sync" not in str(error.value)
    assert error.value.__cause__ is failure


def test_install_copies_full_skill_and_records_notice_once(library, tmp_path):
    # Redistribution retains the original license and attribution checks via --commit.
    source, _, manager, commands = library
    repo = tmp_path / "repo"
    repo.mkdir()
    assert manager.install("engineering/testing-strategy", repo, commit=True) == "testing-strategy"
    destination = repo / ".claude/skills/testing-strategy"
    assert (destination / "SKILL.md").read_bytes() == (source / "engineering/skills/testing-strategy/SKILL.md").read_bytes()
    assert (destination / "references/example.md").read_text() == "Example\n"
    notice = (repo / "THIRD_PARTY_NOTICES.md").read_text()
    assert all(value in notice for value in ("https://example.test/library", "engineering/skills/testing-strategy", "a" * 40, "Apache-2.0 (LICENSE)", "Library attribution"))
    manager.install("knowledge-work-plugins/engineering/testing-strategy", repo, commit=True)
    assert (repo / "THIRD_PARTY_NOTICES.md").read_text() == notice
    (destination / "SKILL.md").write_text("Custom local skill\n")
    with pytest.raises(SkillError, match="different existing skill"):
        manager.install("engineering/testing-strategy", repo, commit=True)
    assert (destination / "SKILL.md").read_text() == "Custom local skill\n"
    assert (repo / "THIRD_PARTY_NOTICES.md").read_text() == notice


def test_spdx_license_is_used(library, tmp_path):
    source, _, manager, _ = library
    (source / "LICENSE").write_text("SPDX-License-Identifier: MIT\nMIT License\n")
    manager.install("engineering/testing-strategy", tmp_path, commit=True)
    assert "License: MIT (LICENSE)" in (tmp_path / "THIRD_PARTY_NOTICES.md").read_text()


@pytest.mark.parametrize("filename,text,license_id", [
    ("LICENSE", "SPDX-License-Identifier: Apache-2.0\n", "Apache-2.0"),
    ("LICENSE.txt", "SPDX-License-Identifier: MIT\n", "MIT"),
    ("LICENSE.txt", "SPDX-License-Identifier: apache-2.0\n", "Apache-2.0"),
    ("LICENSE.txt", STANDARD_LICENSES["BSD-2-Clause"], "BSD-2-Clause"),
    ("LICENSE", STANDARD_LICENSES["BSD-3-Clause"], "BSD-3-Clause"),
    ("LICENSE", "SPDX-License-Identifier: BSD-4-Clause\n", "BSD-4-Clause"),
    ("LICENSE.txt", "SPDX-License-Identifier: 0BSD\n", "0BSD"),
    ("LICENSE", STANDARD_LICENSES["ISC"], "ISC"),
])
def test_skill_license_is_used_before_library_root(library, tmp_path, filename, text, license_id):
    source, _, manager, _ = library
    (source / "engineering/skills/testing-strategy" / filename).write_text(text)
    assert manager.catalog()[0]["license"] == license_id
    repo = tmp_path / "repo"
    manager.install("kwp:engineering/testing-strategy", repo, commit=True)
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
    "Apache License\nVersion 2.0\n", "MIT License\n", "BSD 2-Clause License\n",
    "BSD 3-Clause License\n", "ISC License\n",
    "Permission is hereby granted, free of charge, to any person obtaining a copy\n",
    "Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted.\n",
    "SPDX-License-Identifier:\nMIT License\n", "Example: SPDX-License-Identifier: MIT\n",
    "SPDX-License-Identifier: MIT\nSPDX-License-Identifier: LicenseRef-Custom\n",
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
            action("kwp:engineering/testing-strategy", repo, commit=True)
        assert "LICENSE.txt" in str(error.value)
    assert list(repo.iterdir()) == [repo / "keep.txt"] and commands == []


@pytest.mark.parametrize("license_id", STANDARD_LICENSES)
@pytest.mark.parametrize("variant", ["standard", "whitespace", "copyright", "no-copyright"])
def test_complete_standard_licenses_are_accepted(library, tmp_path, license_id, variant):
    source, _, manager, _ = library
    text = STANDARD_LICENSES[license_id]
    if variant == "copyright":
        text = (text.replace("<year>", "1999-2026").replace("<copyright holders>", "Jin and contributors")
                .replace("<owner>", "Jin and contributors").replace("[yyyy]", "2026")
                .replace("[name of copyright owner]", "Jin"))
    elif variant == "no-copyright":
        text = "\n".join(line for line in text.splitlines() if not line.startswith("Copyright "))
    elif variant == "whitespace":
        text = "\r\n".join("\t " + line.replace(" ", " \t ") + "  " for line in text.splitlines())
    (source / "engineering/skills/testing-strategy/LICENSE.txt").write_text(text)
    assert manager.catalog()[0]["license"] == license_id
    manager.install("kwp:engineering/testing-strategy", tmp_path / "repo", commit=True)
    assert f"License: {license_id}" in (tmp_path / "repo/THIRD_PARTY_NOTICES.md").read_text()


@pytest.mark.parametrize("license_id", STANDARD_LICENSES)
@pytest.mark.parametrize("variant", ["appended", "inserted", "truncated"])
def test_modified_standard_licenses_are_unknown_and_install_writes_nothing(library, tmp_path, license_id, variant):
    source, _, manager, commands = library
    text = STANDARD_LICENSES[license_id]
    restriction = "The Software shall be used for Good, not Evil.\n"
    if variant == "appended":
        text += restriction
    elif variant == "inserted":
        position = text.index("\n\n")
        text = text[:position] + "\n" + restriction + text[position:]
    else:
        text = text[:len(text) // 2]
    (source / "engineering/skills/testing-strategy/LICENSE.txt").write_text(text)
    assert manager.catalog()[0]["license"] == "unknown"
    for action in (manager.check_install, manager.install):
        with pytest.raises(SkillError, match="unknown"):
            action("kwp:engineering/testing-strategy", tmp_path / "repo", commit=True)
    assert not (tmp_path / "repo").exists() and commands == []


def test_json_org_license_is_unknown_and_install_leaves_existing_files_untouched(library, tmp_path):
    source, _, manager, commands = library
    text = STANDARD_LICENSES["MIT"].replace("MIT License", "The JSON License").replace(
        'THE SOFTWARE IS PROVIDED', 'The Software shall be used for Good, not Evil.\n\nTHE SOFTWARE IS PROVIDED',
    )
    (source / "engineering/skills/testing-strategy/LICENSE.txt").write_text(text)
    repo = tmp_path / "repo"
    repo.mkdir()
    notices = repo / "THIRD_PARTY_NOTICES.md"
    notices.write_text("Existing notices\n")
    assert manager.catalog()[0]["license"] == "unknown"
    with pytest.raises(SkillError, match="unknown"):
        manager.install("kwp:engineering/testing-strategy", repo, commit=True)
    assert notices.read_text() == "Existing notices\n"
    assert not (repo / ".claude").exists() and commands == []


@pytest.mark.parametrize("marker", [
    "SPDX-License-Identifier: MIT", "# SPDX-License-Identifier: MIT",
    "// SPDX-License-Identifier: MIT", "/* SPDX-License-Identifier: MIT */",
    " * SPDX-License-Identifier: MIT", "SPDX-License-Identifier: MIT\nSPDX-License-Identifier: mit",
])
def test_spdx_line_takes_precedence_over_prose(library, marker):
    source, _, manager, _ = library
    (source / "engineering/skills/testing-strategy/LICENSE.txt").write_text(
        marker + "\nSource-available. Additional restrictions.\n",
    )
    assert manager.catalog()[0]["license"] == "MIT"


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
        manager.install("kwp:example", tmp_path / "repo", commit=True)
    assert not (tmp_path / "repo").exists()


def test_skill_open_source_license_can_override_a_nonfree_root(library, tmp_path):
    source, _, manager, _ = library
    (source / "LICENSE").write_text("Source-available root terms.\n")
    (source / "engineering/skills/testing-strategy/LICENSE.txt").write_text(STANDARD_LICENSES["MIT"])
    manager.install("engineering/testing-strategy", tmp_path / "repo", commit=True)
    assert manager.catalog()[0]["license"] == "MIT"


def test_license_file_priority_does_not_skip_restrictive_skill_terms(library, tmp_path):
    source, _, manager, _ = library
    skill = source / "engineering/skills/testing-strategy"
    (skill / "LICENSE").write_text("Source-available terms.\n")
    (skill / "LICENSE.txt").write_text("MIT License\n")
    with pytest.raises(SkillError, match="unknown"):
        manager.install("engineering/testing-strategy", tmp_path / "repo", commit=True)


def test_conflicting_skill_or_missing_id_does_not_write(library, tmp_path):
    _, _, manager, _ = library
    with pytest.raises(SkillError, match="unknown skill"):
        manager.install("no/such-skill", tmp_path, commit=True)
    destination = tmp_path / ".claude/skills/testing-strategy"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("different")
    with pytest.raises(SkillError, match="overwrite"):
        manager.install("engineering/testing-strategy", tmp_path, commit=True)
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
    expected = [".foragent.yml", ".claude/agents/fa-api.md", ".gitignore"]
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
    assert ".gitignore" in commits[0]
    assert not any(path.startswith(".claude/skills/") for path in commits[0])


def test_nonfree_lane_skill_installs_locally_with_warning(two_libraries, tmp_path, monkeypatch, capsys):
    manager = two_libraries[3]
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".foragent.yml").write_text("{}\n")
    monkeypatch.setattr(GitRepo, "current_branch", lambda self: "main")
    published = []
    monkeypatch.setattr(GitRepo, "ensure_tracked", lambda self, paths: published.extend(paths))
    monkeypatch.setattr(GitRepo, "commit_paths", lambda *args: None)
    lane = add_lane(root, "design", ["ui/**"], skills=["skills:frontend-design", "skills:docx"],
                    manager=manager, push=False)
    assert lane["skills"] == ["frontend-design", "docx"]
    assert "unknown" in capsys.readouterr().err
    assert (root / ".claude/skills/docx/SKILL.md").is_file()
    assert published == [".foragent.yml", ".claude/agents/fa-design.md", ".gitignore"]


@pytest.fixture
def fixed_lane(two_libraries, tmp_path):
    config = replace(two_libraries[2], skill_roles={
        "skills:frontend-design": "style", "skills:docx": "style",
        "kwp:engineering/testing-strategy": "domain",
    })
    lane = {"paths": ["api/**"], "agent": "custom-api", "skills": ["old-skill"],
            "executor": "claude", "model": "lane-model", "title": "API", "emoji": "🛠️"}
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".foragent.yml").write_text(yaml.safe_dump({
        "language": "ko", "skill_libraries": config.skill_libraries, "skill_roles": config.skill_roles,
        "lanes": {"api": lane, "web": {"paths": ["web/**"], "skills": ["user-skill"]}},
    }))
    agent = root / ".claude/agents/custom-api.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("---\nname: custom-api\ndescription: My API agent\nskills: [old-skill]\n"
                     "memory: project\ntools: [Read, Edit]\n---\nKeep these instructions.\n---\nAnd this section.\n")
    return root, agent, lane, SkillManager(config)


def test_set_lane_skills_replaces_selection_preserving_agent_and_other_settings(fixed_lane):
    root, agent, original, manager = fixed_lane
    before = load_config(root)
    header, body = agent.read_text().split("---", 2)[1:]
    expected = {**original, "skills": ["frontend-design", "testing-strategy"]}
    assert set_lane_skills(root, "api", ["skills:frontend-design", "engineering/testing-strategy"],
                           manager=manager) == expected
    after = load_config(root)
    assert after.lanes == {**before.lanes, "api": expected}
    assert after.language == before.language
    changed_header, changed_body = agent.read_text().split("---", 2)[1:]
    assert yaml.safe_load(changed_header) == {**yaml.safe_load(header), "skills": expected["skills"]}
    assert changed_body == body
    for name in expected["skills"]:
        assert (root / f".claude/skills/{name}/SKILL.md").is_file()
        assert f"/.claude/skills/{name}/" in (root / ".gitignore").read_text().splitlines()

    # Clearing the declarations keeps installed skills available to other lanes.
    assert set_lane_skills(root, "api", [], manager=manager) == {**original, "skills": []}
    assert load_config(root).lanes["api"]["skills"] == []
    assert yaml.safe_load(agent.read_text().split("---", 2)[1])["skills"] == []
    assert (root / ".claude/skills/frontend-design/SKILL.md").is_file()


@pytest.mark.parametrize("identities, message", [
    (["skills:frontend-design", "skills:docx"], "style"),
    (["skills:frontend-design", "missing"], "missing"),
    (["skills:frontend-design", "frontend-design"], "duplicate"),
])
def test_set_lane_skills_invalid_selection_leaves_all_project_files_intact(fixed_lane, identities, message):
    root, _, _, manager = fixed_lane
    before = SkillManager._contents(root)
    with pytest.raises((LaneError, SkillError), match=message):
        set_lane_skills(root, "api", identities, manager=manager)
    assert SkillManager._contents(root) == before


def test_set_lane_skills_unknown_lane_and_invalid_agent_do_not_install(fixed_lane):
    root, agent, _, manager = fixed_lane
    before = SkillManager._contents(root)
    with pytest.raises(LaneError, match="unknown lane"):
        set_lane_skills(root, "missing", ["frontend-design"], manager=manager)
    assert SkillManager._contents(root) == before
    for content in ("No frontmatter.\n", "---\n[]\n---\nBody\n", "---\nskills: [\n---\nBody\n"):
        agent.write_text(content)
        before = SkillManager._contents(root)
        with pytest.raises(LaneError, match="frontmatter"):
            set_lane_skills(root, "api", ["frontend-design"], manager=manager)
        assert SkillManager._contents(root) == before


@pytest.mark.parametrize("agent_state", ["deleted", "never-created", "directory"])
def test_set_lane_skills_unreadable_agent_is_a_clean_error_without_changes(fixed_lane, agent_state, capsys):
    root, agent, _, manager = fixed_lane
    if agent_state == "never-created":
        settings = root / ".foragent.yml"
        local = yaml.safe_load(settings.read_text())
        local["lanes"]["api"]["agent"] = "never-created"
        settings.write_text(yaml.safe_dump(local))
        agent = root / ".claude/agents/never-created.md"
    else:
        agent.unlink()
        if agent_state == "directory":
            agent.mkdir()
    before = SkillManager._contents(root)
    with pytest.raises(LaneError, match="cannot read lane agent"):
        set_lane_skills(root, "api", ["frontend-design"], manager=manager)
    assert cli.main(["lanes", "skills", "api", "skills:frontend-design", "--repo-root", str(root)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "cannot read lane agent" in output.err and str(agent) in output.err
    assert "Traceback" not in output.err
    assert SkillManager._contents(root) == before
    assert not (root / ".claude/skills").exists()


def test_set_lane_skills_does_not_copy_overridden_global_lanes(fixed_lane, tmp_path, monkeypatch):
    root, _, original, manager = fixed_lane
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    global_path = home / ".config/foragent/config.yml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text(yaml.safe_dump({"lanes": {
        "global-only": {"paths": ["global/**"], "skills": []},
        "api": {"paths": ["global-api/**"], "agent": "global-api"},
    }}))
    global_before = global_path.read_bytes()
    settings = root / ".foragent.yml"
    local_before = yaml.safe_load(settings.read_text())
    assert load_config(root).lanes == local_before["lanes"]
    expected = {**original, "skills": ["frontend-design"]}
    assert set_lane_skills(root, "api", ["frontend-design"], manager=manager) == expected
    local_after = yaml.safe_load(settings.read_text())
    assert local_after == {**local_before, "lanes": {**local_before["lanes"], "api": expected}}
    assert load_config(root).lanes == local_after["lanes"]
    assert global_path.read_bytes() == global_before


def test_set_lane_skills_conflicting_install_names_do_not_partly_install(fixed_lane):
    root, _, _, manager = fixed_lane
    other = manager.libraries[0].path / "other/skills/testing-strategy"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: other-testing\ndescription: Different skill\n---\nOther body.\n")
    before = SkillManager._contents(root)
    with pytest.raises(LaneError, match="conflicting installation names"):
        set_lane_skills(root, "api", ["kwp:engineering/testing-strategy", "kwp:other/testing-strategy"],
                        manager=manager)
    assert SkillManager._contents(root) == before


def test_lane_add_also_rejects_two_style_skills_before_writes(fixed_lane):
    root, _, _, manager = fixed_lane
    before = SkillManager._contents(root)
    with pytest.raises(LaneError, match="style"):
        add_lane(root, "design", ["ui/**"], skills=["frontend-design", "docx"], manager=manager)
    assert SkillManager._contents(root) == before


def test_lane_skills_cli_reports_replacement_and_clear(fixed_lane, capsys):
    root, _, _, _ = fixed_lane
    assert cli.main(["lanes", "skills", "api", "skills:frontend-design", "--repo-root", str(root)]) == 0
    assert "Updated lane:api skills: frontend-design" in capsys.readouterr().out
    assert cli.main(["lanes", "skills", "api", "--repo-root", str(root)]) == 0
    assert "Updated lane:api skills: (none)" in capsys.readouterr().out


def test_nonfree_install_cli_explains_refusal_and_catalog_still_lists_it(two_libraries, tmp_path, monkeypatch, capsys):
    manager = two_libraries[3]
    monkeypatch.setattr(cli, "SkillManager", lambda config: manager)
    assert cli.main(["skills", "catalog", "--json"]) == 0
    assert any(row["id"] == "skills:docx" and row["license"] == "unknown"
               for row in json.loads(capsys.readouterr().out))
    assert cli.main(["skills", "install", "skills:docx", "--repo-root", str(tmp_path / "repo"), "--commit"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "unknown" in output.err and "skills:docx" in output.err
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


@pytest.mark.parametrize("license_bytes", [b"SPDX-License-Identifier: Proprietary\n", b"\xff\xfe"])
def test_local_install_ignores_only_its_directory_and_does_not_publish_notices(library, tmp_path, license_bytes, capsys):
    source, _, manager, commands = library
    (source / "LICENSE").write_bytes(license_bytes)
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_bytes(b"# Existing rules\r\nbuild/")
    (root / "THIRD_PARTY_NOTICES.md").write_text("User notices\n")
    for _ in range(2):
        assert manager.install("kwp:engineering/testing-strategy", root) == "testing-strategy"
    assert (root / ".gitignore").read_bytes() == b"# Existing rules\r\nbuild/\n/.claude/skills/testing-strategy/\n"
    assert (root / "THIRD_PARTY_NOTICES.md").read_text() == "User notices\n"
    assert "Warning:" in capsys.readouterr().err
    assert commands == []


def test_copy_lane_skills_prefers_local_then_cache_and_preserves_checkout(library, tmp_path):
    source, config, manager, _ = library
    root, worktree = tmp_path / "repo", tmp_path / "worktree"
    skill = "engineering/testing-strategy"
    manager.install(skill, root)
    local = root / ".claude/skills/testing-strategy/SKILL.md"
    local.write_text("Local customization\n")
    copied = copy_lane_skills(root, worktree, ["testing-strategy"], manager)
    assert copied == [".claude/skills/testing-strategy"]
    assert (worktree / copied[0] / "SKILL.md").read_text() == "Local customization\n"
    (worktree / copied[0] / "SKILL.md").write_text("Checkout skill\n")
    assert copy_lane_skills(root, worktree, ["testing-strategy"], manager) == []
    assert (worktree / copied[0] / "SKILL.md").read_text() == "Checkout skill\n"
    # A missing unrelated library must not prevent using a synced cache.
    config.skill_libraries.append({"name": "missing", "repo": "local", "path": str(tmp_path / "missing")})
    cached = tmp_path / "cached-worktree"
    assert copy_lane_skills(tmp_path / "fresh-repo", cached, ["testing-strategy"], SkillManager(config)) == copied
    assert (cached / copied[0] / "references/example.md").read_text() == "Example\n"
    assert (cached / copied[0] / "SKILL.md").read_bytes() == (source / "engineering/skills/testing-strategy/SKILL.md").read_bytes()


@pytest.mark.parametrize("text, expected", [
    ("# top\nrepo:    o/r  # keep\n",
     "# top\nrepo:    o/r  # keep\nlanes:\n  docs:\n    paths: [docs/**]\n"),
    ("lanes:\n    api:\n        paths:\n          - 'api/**'\n\n# tail\nmax_parallel: 1\n",
     "lanes:\n    api:\n        paths:\n          - 'api/**'\n    docs:\n        paths: [docs/**]\n"
     "\n# tail\nmax_parallel: 1\n"),
    ("lanes:\n  api:\n    paths:\n    - api/**\n# tail\n",
     "lanes:\n  api:\n    paths:\n    - api/**\n  docs:\n    paths: [docs/**]\n# tail\n"),
    ("lanes: {}  # none yet\nrepo: o/r\n",
     "lanes: {docs: {paths: [docs/**]}}  # none yet\nrepo: o/r\n"),
    ("# top\n{}\n", "# top\n{lanes: {docs: {paths: [docs/**]}}}\n"),
    ("repo: o/r\n# keep tail", "repo: o/r\nlanes:\n  docs:\n    paths: [docs/**]\n# keep tail"),
    ("repo: o/r\n\n# keep tail\n", "repo: o/r\nlanes:\n  docs:\n    paths: [docs/**]\n\n# keep tail\n"),
    ("# only a note", "lanes:\n  docs:\n    paths: [docs/**]\n# only a note"),
    ("# head\nrepo: owner/repo", "# head\nlanes:\n  docs:\n    paths: [docs/**]\nrepo: owner/repo"),
    ("lanes:\n  api:\n    paths: [api/**]", "lanes:\n  docs:\n    paths: [docs/**]\n  api:\n    paths: [api/**]"),
    ("# only a note\n", "# only a note\nlanes:\n  docs:\n    paths: [docs/**]\n"),
    ("lanes: {api: {paths: [api/**]},  # api\n  web: {paths: [web/**]}}  # end\n",
     "lanes: {api: {paths: [api/**]},  # api\n  web: {paths: [web/**]}, docs: {paths: [docs/**]}}  # end\n"),
    ("lanes:   # soon\nrepo: o/r\n", "lanes:   # soon\n  docs:\n    paths: [docs/**]\nrepo: o/r\n"),
    ("repo:    o/r\r\nlanes:\r\n  api:  # a \r\n    paths: [api/**]\r\n# tail\r\n",
     "repo:    o/r\r\nlanes:\r\n  api:  # a \r\n    paths: [api/**]\r\n  docs:\r\n    paths: [docs/**]\r\n# tail\r\n"),
    ("{repo: o/r}  # flow\n", "{repo: o/r, lanes: {docs: {paths: [docs/**]}}}  # flow\n"),
])
def test_settings_document_adds_a_lane_without_moving_other_lines(tmp_path, text, expected):
    from foragent.config import LaneSettings

    path = tmp_path / ".foragent.yml"
    path.write_bytes(text.encode())
    settings = LaneSettings(path)
    settings.add_lane("docs", {"paths": ["docs/**"]}, {})
    settings.save()
    assert path.read_bytes() == expected.encode()


@pytest.mark.parametrize("text, skills, expected", [
    ("lanes:\n  api:\n    skills:\n    - tone\n    agent: fa-api  # keep\n# tail\n", ["polish"],
     "lanes:\n  api:\n    skills:\n    - polish\n    agent: fa-api  # keep\n# tail\n"),
    ("repo:    o/r\nlanes:\n  api:\n    skills: [tone]  # fixed\n", ["polish", "x"],
     "repo:    o/r\nlanes:\n  api:\n    skills: [polish, x]  # fixed\n"),
    ("lanes:\n  api:\n    skills:\n    - tone # Keep tone note\n    - keep\n", ["keep", "polish"],
     "lanes:\n  api:\n    skills:\n           # Keep tone note\n    - keep\n    - polish\n"),
    ("lanes:\n  api:\n    skills:\n    - tone   #  spaced note  \n", ["polish"],
     "lanes:\n  api:\n    skills:\n             #  spaced note  \n    - polish\n"),
    ("lanes:\n  api: {paths: [api/**],\n    # why\n    skills: [tone]}  # c\n", ["polish"],
     "lanes:\n  api: {paths: [api/**],\n    # why\n    skills: [polish]}  # c\n"),
    ("lanes:\n  api: {paths: [api/**],  # why\n    agent: fa-api}\n", ["polish"],
     "lanes:\n  api: {paths: [api/**],  # why\n    agent: fa-api, skills: [polish]}\n"),
    ("lanes:\r\n  api:\r\n    skills: [tone]  # c \r\n    agent: fa-api\r\n", [],
     "lanes:\r\n  api:\r\n    skills: []  # c \r\n    agent: fa-api\r\n"),
    ("lanes:\n  api:\n    skills:\n    - tone\n# trailing\nmax_parallel: 1\n", [],
     "lanes:\n  api:\n    skills: []\n# trailing\nmax_parallel: 1\n"),
    ("lanes:\n  api:\n    paths: [api/**]\n\n# tail\n", ["polish"],
     "lanes:\n  api:\n    paths: [api/**]\n    skills: [polish]\n\n# tail\n"),
    ("lanes:\n  api:\n    paths: [api/**]", ["polish"],
     "lanes:\n  api:\n    skills: [polish]\n    paths: [api/**]"),
    ("lanes:\n  api:\n    skills:\n    agent: fa-api\n", ["polish"],
     "lanes:\n  api:\n    skills: [polish]\n    agent: fa-api\n"),
])
def test_settings_document_replaces_a_block_list_in_place(tmp_path, text, skills, expected):
    from foragent.config import LaneSettings

    path = tmp_path / ".foragent.yml"
    path.write_bytes(text.encode())
    settings = LaneSettings(path)
    settings.set_skills("api", skills, {}, {})
    settings.save()
    assert path.read_bytes() == expected.encode()


def test_settings_document_refuses_an_edit_that_would_change_other_settings(tmp_path):
    from foragent.config import ConfigError, LaneSettings

    path = tmp_path / ".foragent.yml"
    text = "base: &shared\n  skills: [tone]\nlanes:\n  api: *shared\n"
    path.write_text(text)
    settings = LaneSettings(path)
    settings.set_skills("api", ["polish"], {}, {})
    with pytest.raises(ConfigError, match="edit it by hand"):
        settings.save()
    assert path.read_text() == text


@pytest.mark.parametrize("text", [
    "lanes:\n  api:\n    skills: [tone, # preserve me\n      handmade]\n    agent: fa-api\n",
    "lanes:\n  api:\n    skills:\n    - >- # comment\n      tone\n    agent: fa-api\n",
    "lanes:\n  api:\n    skills:\n    - [tone]\n",
])
def test_settings_document_refuses_unusual_skills_layouts_by_lane(tmp_path, text):
    from foragent.config import ConfigError, LaneSettings

    path = tmp_path / ".foragent.yml"
    path.write_bytes(text.encode())
    with pytest.raises(ConfigError, match="lane api: .*edit .foragent.yml by hand"):
        LaneSettings(path).set_skills("api", ["polish"], {}, {})
    assert path.read_bytes() == text.encode()


def test_settings_document_never_writes_a_result_that_drops_a_comment(tmp_path):
    from foragent.config import ConfigError, LaneSettings

    path = tmp_path / ".foragent.yml"
    text = "lanes:\n  api:\n    skills: [tone]\n    # Keep this  \n    agent: fa-api\n"
    path.write_bytes(text.encode())
    settings = LaneSettings(path)
    settings.set_skills("api", ["polish"], {}, {})
    del settings.lines[3]
    with pytest.raises(ConfigError, match="changing or losing a comment"):
        settings.save()
    assert path.read_bytes() == text.encode()


def test_settings_document_refuses_to_change_a_final_comment_line_end(tmp_path):
    from foragent.config import ConfigError, LaneSettings

    path = tmp_path / ".foragent.yml"
    text = "lanes:   # soon"
    path.write_bytes(text.encode())
    settings = LaneSettings(path)
    with pytest.raises(ConfigError, match="edit it by hand"):
        settings.add_lane("docs", {"paths": ["docs/**"]}, {})
        settings.save()
    assert path.read_bytes() == text.encode()


@pytest.mark.parametrize("text, skills", [
    ("lanes:\n  api:\n    skills:\n    - tone", ["tone", "polish"]),
    ("repo: o/r\nlanes:", None),
])
def test_settings_document_refuses_to_add_a_line_end_to_the_last_setting(tmp_path, text, skills):
    from foragent.config import ConfigError, LaneSettings

    path = tmp_path / ".foragent.yml"
    path.write_bytes(text.encode())
    settings = LaneSettings(path)
    with pytest.raises(ConfigError, match="no line end; edit it by hand"):
        if skills is None:
            settings.add_lane("docs", {"paths": ["docs/**"]}, {})
        else:
            settings.set_skills("api", skills, {}, {})
    assert path.read_bytes() == text.encode()


def test_repo_product_skill_wins_by_name_over_library_for_install_and_copy(library, tmp_path):
    source, config, manager, _ = library
    root, worktree = tmp_path / "repo", tmp_path / "worktree"
    product = root / "skills/testing-strategy"
    product.mkdir(parents=True)
    (product / "SKILL.md").write_text("---\nname: testing-strategy\ndescription: Product copy\n---\nRepo body.\n")
    (product / "LICENSE").write_text("SPDX-License-Identifier: MIT\nCopyright (c) 2026 Example\n")
    skill = manager.check_install("testing-strategy", root)
    assert (skill.path, skill.license, skill.description) == (product, "MIT", "Product copy")
    assert manager.install("testing-strategy", root) == "testing-strategy"
    assert (root / ".claude/skills/testing-strategy/SKILL.md").read_text().endswith("Repo body.\n")
    # A fully qualified library ID still selects the library copy.
    assert manager.check_install("kwp:engineering/testing-strategy", tmp_path / "other").path == (
        source / "engineering/skills/testing-strategy")
    (root / ".claude/skills/testing-strategy/SKILL.md").write_text("Stale local install\n")
    assert copy_lane_skills(root, worktree, ["testing-strategy"], manager) == [".claude/skills/testing-strategy"]
    assert (worktree / ".claude/skills/testing-strategy/SKILL.md").read_text().endswith("Repo body.\n")
