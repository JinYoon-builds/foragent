"""Role-scoped default skill sections, and the install and license checks behind them (#1619)."""

from pathlib import Path

import pytest

from foragent.config import Config
from foragent.executors import claude_review_command
from foragent.run_skill_catalog import CATALOG_INLINE_BYTES, available_skills, catalog_section
from foragent.session_skills import SECTION_BYTES, assigned_names, section
from foragent.skills import SkillError, SkillManager, trusted_source


def write_skill(base: Path, name: str, description: str = "About it", license: str | None = "MIT") -> Path:
    skill = base / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: {name}\ndescription: {description}\n---\nBody\n")
    if license:
        (skill.parent / "LICENSE").write_text(f"SPDX-License-Identifier: {license}\n")
    return skill


def test_unset_empty_and_null_roles_add_nothing_and_never_borrow(tmp_path):
    write_skill(tmp_path, "theirs")
    for table in ({}, None, {"workers": ["theirs"]}, {"workers": ["theirs"], "docs": []},
                  {"workers": ["theirs"], "docs": None}):
        assert section(Config(default_skills=table), "docs", tmp_path) == ""


def test_section_names_the_skill_description_and_a_path_the_session_can_read(tmp_path):
    root, worktree = tmp_path / "root", tmp_path / "worktree"
    write_skill(worktree, "in-checkout")
    write_skill(root, "in-project")
    config = Config(default_skills={"docs": ["in-checkout", "in-project", "in-checkout"]}, skill_libraries=[])
    value = section(config, "docs", root, worktree)
    assert value.startswith("\n\n## Default skills") and value.endswith("\n")
    assert "- in-checkout: About it (skills/in-checkout/SKILL.md)" in value
    assert f"- in-project: About it ({root}/skills/in-project/SKILL.md)" in value
    assert value.count("in-checkout:") == 1, "A repeated entry lists once"


def test_section_says_when_a_skill_is_unavailable_and_never_substitutes(tmp_path):
    write_skill(tmp_path, "other")
    config = Config(default_skills={"docs": ["gone"], "workers": ["other"]}, skill_libraries=[])
    value = section(config, "docs", tmp_path)
    assert "- gone: not available here" in value and "other" not in value


def test_a_cached_library_copy_needs_license_facts_but_a_full_id_reaches_a_permitted_one(tmp_path):
    library = tmp_path / "library"
    allowed = write_skill(library, "allowed")
    write_skill(library, "restricted", license="LicenseRef-Source-Available")
    config = Config(skill_libraries=[{"name": "lib", "repo": "unused", "path": str(library)}],
                    default_skills={"docs": ["lib:allowed", "restricted"]})
    value = section(config, "docs", tmp_path / "project")
    assert str(allowed.resolve()) in value
    assert "- restricted: not available here" in value and "Source-Available" not in value


def test_twelve_long_descriptions_stay_within_the_bound_and_all_remain_listed(tmp_path):
    names = [f"skill-{n:02d}" for n in range(12)]
    for name in names:
        write_skill(tmp_path, name, description="가" * 3000)  # A multibyte cut must stay valid text.
    value = section(Config(default_skills={"docs": names}, skill_libraries=[]), "docs", tmp_path)
    assert len(value.encode("utf-8")) <= SECTION_BYTES
    assert len(value.encode("utf-8")) > SECTION_BYTES - 200, "Descriptions use the room they are given"
    assert all(f"skills/{name}/SKILL.md" in value for name in names)


def test_short_descriptions_are_never_cut(tmp_path):
    write_skill(tmp_path, "short", description="Whole sentence stays.")
    value = section(Config(default_skills={"docs": ["short"]}, skill_libraries=[]), "docs", tmp_path)
    assert "Whole sentence stays." in value and "..." not in value


def test_assigned_names_use_the_installed_directory_of_a_full_id():
    config = Config(default_skills={"docs": ["kwp:engineering/one"], "workers": ["two"], "architect": []})
    assert assigned_names(config) == {"one", "two"}


@pytest.mark.parametrize("repo,trusted", [
    ("https://github.com/anthropics/skills", True),
    ("https://github.com/anthropics/skills.git", True),
    ("https://github.com/Anthropics/Skills/", True),
    ("git@github.com:anthropics/knowledge-work-plugins.git", True),
    ("https://github.com/unknown-publisher/skills.git", False),
    ("https://github.com/anthropics/skills-extra", False),
])
def test_trust_follows_the_repository_address(repo, trusted):
    assert trusted_source(repo) is trusted


def catalog_config(tmp_path, repo, **values):
    library = tmp_path / "library"
    write_skill(library, "open-skill")
    write_skill(library, "restricted", license="LicenseRef-Source-Available")
    write_skill(library, "assigned")
    return Config(skill_libraries=[{"name": "any-name", "repo": repo, "path": str(library)}], **values)


def test_a_trusted_source_offers_only_skills_under_a_redistributable_license(tmp_path):
    config = catalog_config(tmp_path, "https://github.com/anthropics/skills.git")
    assert [name for name, _, _ in available_skills(config, [])] == ["assigned", "open-skill"]


def test_an_unfamiliar_source_needs_a_person_to_confirm_a_role(tmp_path):
    config = catalog_config(tmp_path, "https://github.com/unknown-publisher/skills.git",
                            skill_roles={"any-name:restricted": "tool"})
    assert [name for name, _, _ in available_skills(config, [])] == ["restricted"]


def test_any_roles_default_leaves_the_catalog_even_when_a_person_confirmed_a_role(tmp_path):
    config = catalog_config(tmp_path, "https://github.com/anthropics/skills", default_skills={"docs": ["assigned"]},
                            skill_roles={"any-name:assigned": "tool"})
    assert [name for name, _, _ in available_skills(config, [])] == ["open-skill"]


def test_a_long_catalog_moves_to_a_file_and_a_short_one_stays_inline(tmp_path):
    long = [(f"name-{n}", "x" * 200, f"/s/{n}/SKILL.md") for n in range(CATALOG_INLINE_BYTES // 200)]
    value = catalog_section(long, tmp_path)
    saved = tmp_path / "available-skills.md"
    assert str(saved) in value and "name-0:" not in value and "Skills used: <names>" in value
    assert all(f"- {name}: {description} ({path})" in saved.read_text() for name, description, path in long)
    short = catalog_section(long[:2], tmp_path / "other")
    assert "- name-0:" in short and not (tmp_path / "other").exists()


def manager_for(tmp_path, **licenses) -> SkillManager:
    library = tmp_path / "library"
    for name, license in licenses.items():
        write_skill(library, name, license=license)
    return SkillManager(Config(skill_libraries=[{"name": "lib", "repo": "unused", "path": str(library),
                                                  "revision": "pinned", "skills": list(licenses)}]))


def test_check_default_refuses_missing_license_facts_without_changing_the_project(tmp_path):
    manager = manager_for(tmp_path, none=None, restricted="LicenseRef-Restricted")
    project = tmp_path / "project"
    project.mkdir()
    for name in ("none", "restricted"):
        with pytest.raises(SkillError, match=f"{name}.*license"):
            manager.check_default(name, project)
    assert list(project.iterdir()) == []


def test_install_default_keeps_a_persons_changed_copy_on_a_second_setup(tmp_path):
    manager = manager_for(tmp_path, kept="MIT")
    project = tmp_path / "project"
    project.mkdir()
    skill = manager.check_default("kept", project)
    assert manager.default_needs_ignore(skill, project)
    manager.install("kept", project)
    copy = project / ".claude/skills/kept/SKILL.md"
    assert copy.is_file() and (project / ".gitignore").read_text() == "/.claude/skills/kept/\n"
    assert not manager.default_needs_ignore(skill, project)
    copy.write_text("edited\n")
    assert manager.check_default("kept", project) is None, "A differing copy stays and does not stop setup"
    assert copy.read_text() == "edited\n"


def test_check_default_judges_the_copy_that_stays_not_the_library(tmp_path):
    manager = manager_for(tmp_path, listed="MIT")
    project = tmp_path / "project"
    write_skill(project / ".claude", "listed", license="LicenseRef-Source-Available")
    with pytest.raises(SkillError, match="listed.*license"):
        manager.check_default("listed", project)
    write_skill(project / ".claude", "own-copy")
    assert manager.check_default("own-copy", project) is None, "A permitted copy needs no library entry"
    with pytest.raises(SkillError, match="unknown skill"):
        manager.check_default("nowhere", project)


@pytest.mark.parametrize("folder", [".claude/skills", "skills"])
@pytest.mark.parametrize("where", ["checkout", "project"])
def test_a_local_copy_without_redistributable_license_facts_never_reaches_a_session(tmp_path, folder, where):
    root, worktree = tmp_path / "root", tmp_path / "worktree"
    base = (worktree if where == "checkout" else root) / folder.removesuffix("skills").rstrip("/")
    write_skill(base, "restricted", license="LicenseRef-Source-Available")
    write_skill(base, "silent", license=None)
    write_skill(base, "open-one")
    config = Config(default_skills={"docs": ["restricted", "silent", "open-one"]}, skill_libraries=[])
    value = section(config, "docs", root, worktree)
    assert "- restricted: not available here" in value and "- silent: not available here" in value
    assert "restricted/SKILL.md" not in value and "silent/SKILL.md" not in value
    assert "open-one/SKILL.md" in value


def test_a_restricted_copy_does_not_hide_a_permitted_one_further_along(tmp_path):
    root, worktree = tmp_path / "root", tmp_path / "worktree"
    write_skill(worktree, "both", license="LicenseRef-Source-Available")
    write_skill(root, "both")
    value = section(Config(default_skills={"docs": ["both"]}, skill_libraries=[]), "docs", root, worktree)
    assert f"({root}/skills/both/SKILL.md)" in value


def test_an_installed_copy_without_its_own_file_keeps_its_librarys_license_facts(tmp_path):
    root = tmp_path / "root"
    library = tmp_path / "library"
    write_skill(library, "borrowed", license=None)
    (library / "LICENSE").write_text("SPDX-License-Identifier: LicenseRef-Source-Available\n")
    write_skill(root / ".claude", "borrowed", license=None)
    (root / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")  # The project's MIT cannot relicense it.
    entry = {"name": "lib", "repo": "unused", "path": str(library)}
    config = Config(default_skills={"docs": ["borrowed"]}, skill_libraries=[entry])
    assert "- borrowed: not available here" in section(config, "docs", root)
    (library / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
    assert ".claude/skills/borrowed/SKILL.md" in section(config, "docs", root)


def test_native_review_gets_added_text_only_when_there_is_some():
    assert "--append-system-prompt" not in claude_review_command("low")
    assert "--append-system-prompt" not in claude_review_command("low", append_system_prompt="")
    args = claude_review_command("low", pr=7, append_system_prompt="Added")
    assert args[args.index("--append-system-prompt") + 1] == "Added" and "/code-review 7 low" in args


def test_a_closer_restrictive_license_wins_over_the_project_license(tmp_path):
    root = tmp_path / "root"
    write_skill(root, "product", license=None)
    (root / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
    (root / "skills/LICENSE").write_text("SPDX-License-Identifier: LicenseRef-Source-Available\n")
    config = Config(default_skills={"docs": ["product"]}, skill_libraries=[])
    assert "- product: not available here" in section(config, "docs", root)
    with pytest.raises(SkillError, match="product.*license"):
        SkillManager(config).check_default("product", root)
    assert not (root / ".claude").exists()
    (root / "skills/LICENSE").unlink()
    assert "skills/product/SKILL.md" in section(config, "docs", root)
    assert SkillManager(config).check_default("product", root) is not None


@pytest.mark.parametrize("where", ["checkout", "project"])
def test_a_library_license_never_overrides_a_restrictive_license_beside_the_copy(tmp_path, where):
    root, worktree = tmp_path / "root", tmp_path / "worktree"
    library = tmp_path / "library"
    write_skill(library, "shared")  # The same name, MIT, in a library.
    base = worktree if where == "checkout" else root
    write_skill(base / ".claude", "shared", license=None)
    (base / ".claude/skills/LICENSE").write_text("SPDX-License-Identifier: LicenseRef-Source-Available\n")
    config = Config(default_skills={"docs": ["shared"]},
                    skill_libraries=[{"name": "lib", "repo": "unused", "path": str(library)}])
    value = section(config, "docs", root, worktree)
    assert ".claude/skills/shared" not in value, "The restricted copy never reaches a session"
    assert str((library / "skills/shared/SKILL.md").resolve()) in value, "The MIT original still may"
    if where == "project":
        with pytest.raises(SkillError, match="shared.*license"):
            SkillManager(config).check_default("shared", root)


@pytest.mark.parametrize("kind", ["folder", "file", "inside"])
@pytest.mark.parametrize("folder", [".claude/skills", "skills"])
@pytest.mark.parametrize("where", ["checkout", "project"])
def test_a_default_reached_through_a_symlink_never_reaches_a_session(tmp_path, kind, folder, where):
    root, worktree = tmp_path / "root", tmp_path / "worktree"
    elsewhere = tmp_path / "elsewhere"
    write_skill(elsewhere, "linked")  # MIT where it lives, and the project is MIT too.
    base = worktree if where == "checkout" else root
    (base / folder).mkdir(parents=True)
    (base / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
    target = base / folder / "linked"
    if kind == "folder":
        target.symlink_to(elsewhere / "skills/linked")
    else:
        target.mkdir()
        (target / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
        if kind == "file":
            (target / "SKILL.md").symlink_to(elsewhere / "skills/linked/SKILL.md")
        else:
            (target / "SKILL.md").write_text("---\nname: linked\ndescription: About it\n---\nBody\n")
            (target / "notes.md").symlink_to(elsewhere / "skills/linked/SKILL.md")
    value = section(Config(default_skills={"docs": ["linked"]}, skill_libraries=[]), "docs", root, worktree)
    assert "- linked: not available here" in value and "linked/SKILL.md" not in value
    if where == "project" and folder == ".claude/skills":
        with pytest.raises(SkillError, match="symlink"):
            SkillManager(Config(skill_libraries=[])).check_default("linked", root)


@pytest.mark.parametrize("link", ["skill", "parent"])
def test_a_library_skill_reached_through_a_symlinked_folder_is_no_default_but_stays_in_the_catalog(tmp_path, link):
    library, elsewhere = tmp_path / "library", tmp_path / "elsewhere"
    write_skill(elsewhere, "linked")
    library.mkdir()
    if link == "skill":
        (library / "skills").mkdir()
        (library / "skills/linked").symlink_to(elsewhere / "skills/linked")
    else:  # Only a folder above the skill is a link; the skill folder itself is not.
        (library / "skills").symlink_to(elsewhere / "skills")
    (library / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
    entry = {"name": "lib", "repo": "https://github.com/anthropics/skills", "path": str(library)}
    config = Config(skill_libraries=[entry], default_skills={"docs": ["linked"]})
    assert "- linked: not available here" in section(config, "docs", tmp_path / "project")
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(SkillError, match="symlink"):
        SkillManager(config).check_default("linked", project)
    assert not (project / ".claude").exists()
    catalog = Config(skill_libraries=[entry])
    assert [name for name, _, _ in available_skills(catalog, [])] == ["linked"]


from foragent.license_texts import STANDARD_LICENSES  # noqa: E402

JANE_MIT = STANDARD_LICENSES["MIT"].replace("<year> <copyright holders>", "2026 Jane Doe")
BANNED_HOLDER_MIT = STANDARD_LICENSES["MIT"].replace(
    "<year> <copyright holders>", "2026 Jane Doe. Redistribution is prohibited.")

RESTRICTED_TEXTS = {
    "standard-then-ban": STANDARD_LICENSES["MIT"] + "\nRedistribution of this skill is prohibited.\n",
    "spdx-then-ban": "SPDX-License-Identifier: MIT\nRedistribution of this skill is prohibited.\n",
    "two-spdx": "SPDX-License-Identifier: MIT\nSPDX-License-Identifier: LicenseRef-Private\n",
    "spdx-then-author": "SPDX-License-Identifier: MIT\nAuthor: Example\n",
    "heading-variant": STANDARD_LICENSES["MIT"].replace("MIT License", "The MIT License (MIT)", 1),
    "missing-words": STANDARD_LICENSES["MIT"].replace("free of charge, ", ""),
    "cut-short": STANDARD_LICENSES["MIT"].split("THE SOFTWARE IS PROVIDED")[0],
    "ban-on-the-next-line": STANDARD_LICENSES["MIT"].replace(
        "<year> <copyright holders>", "2026 Example\nRedistribution is prohibited."),
    "copyleft": "SPDX-License-Identifier: GPL-3.0-only\n",
    "non-commercial": "SPDX-License-Identifier: CC-BY-NC-4.0\nCopyright 2026 Example\n",
    "only-copyright": "Copyright 2026 Example\n",
    # #1652: an attribution or source notice never carries an unchecked license or a sentence.
    "attribution-noncommercial-name": "example by Jane Doe, BSD-NonCommercial License, Copyright (c) 2026 Jane Doe\n"
    + STANDARD_LICENSES["MIT"],
    "attribution-license-in-author": "example by Jane Doe BSD-NonCommercial License\n" + STANDARD_LICENSES["MIT"],
    "attribution-spdx-noncommercial": "example by Jane Doe, CC-BY-NC-4.0\n" + STANDARD_LICENSES["MIT"],
    "labelled-license": "Attribution: BSD-NonCommercial License\n" + STANDARD_LICENSES["MIT"],
    "labelled-sentence": "Source: Example Project\nAttribution: no commercial use\n" + STANDARD_LICENSES["MIT"],
    "sentence-as-work": "Redistribution is prohibited by Jane Doe\n" + STANDARD_LICENSES["MIT"],
    "labelled-copyleft-after-comma": "Attribution: Jane Doe, GPL-3.0-only\n" + STANDARD_LICENSES["MIT"],
    "attribution-noncommercial-no-space": "example by Jane Doe,CC-BY-NC-4.0\n" + STANDARD_LICENSES["MIT"],
    "identifier-as-name-word": "Attribution: Jane Doe GPL-3.0-only\n" + STANDARD_LICENSES["MIT"],
    "one-word-attribution-is-no-work": "Attribution: noncommercial\n" + STANDARD_LICENSES["MIT"],
    # #1652 review 3: a name the file's copyright lines or URLs do not carry is unsure and fails safe.
    "title-case-ban": "Attribution: Redistribution Is Prohibited.\n" + JANE_MIT,
    "title-case-source-ban": "Source: No Commercial Use\n" + JANE_MIT,
    "title-case-work-ban": "No Commercial Use by Jane Doe, MIT License\n" + JANE_MIT,
    "unbacked-author": "pstack by Someone Else\nhttps://example.org/pstack\n" + JANE_MIT,
    "work-without-url": "Source: pstack\n" + JANE_MIT,
    "holder-then-condition": "Attribution: Jane Doe, Noncommercial\n" + JANE_MIT,
    # #1652 review 4: only a whole holder backs a notice, never a part of a copyright line.
    "condition-from-copyright-line": "Attribution: Redistribution is prohibited.\n" + BANNED_HOLDER_MIT,
    "holder-sentence-repeated": "Attribution: Jane Doe. Redistribution is prohibited.\n" + BANNED_HOLDER_MIT,
    "name-cut-from-a-longer-holder": "Attribution: Jane Doe\n" + BANNED_HOLDER_MIT,
    "part-of-a-holder": "Attribution: Doe\n" + JANE_MIT,
    "part-of-a-url-part": "Source: pst\nhttps://example.org/pstack\n" + JANE_MIT,
    # #1652 review 5: a URL with any part that names a license is no notice, on every label and alone.
    "license-in-url-and-label": "Source: Jane,GPL-3.0-only\nhttps://example.org/Jane,GPL-3.0-only\n"
    + STANDARD_LICENSES["MIT"].replace("<year> <copyright holders>", "2026 Jane"),
    "license-url-alone": "https://example.org/Jane,GPL-3.0-only\n" + JANE_MIT,
    "upstream-copyleft-url": "Upstream: https://example.org/GPL-3.0-only\n" + JANE_MIT,
    "homepage-noncommercial-url": "Homepage: https://example.org/CC-BY-NC/home\n" + JANE_MIT,
    "origin-license-query": "Origin: https://example.org/get?license=nc\n" + JANE_MIT,
    "repository-capitals-identifier": "Repository: https://example.org/AGPL\n" + JANE_MIT,
    "identifier-as-work": "GPL-3.0-only by Jane Doe\nhttps://example.org/GPL-3.0-only\n" + JANE_MIT,
    # #1652 review 6: escapes and case cannot hide a license in a URL, alone, on a label or as a work.
    "escaped-license-label": "Attribution: https://creativecommons.org/%6cicenses/by-nc/4.0/\n" + JANE_MIT,
    "escaped-license-alone": "https://creativecommons.org/%6cicenses/by-nc/4.0/\n" + JANE_MIT,
    "lowercase-cc-terms": "Upstream: https://creativecommons.org/by-nc/4.0/\n" + JANE_MIT,
    "mixed-case-family": "Homepage: https://example.org/Gpl\n" + JANE_MIT,
    "fullwidth-letters": "Source: https://example.org/\uff4cicense\n" + JANE_MIT,
    "escaped-work": "nebula by Jane Doe\nhttps://example.org/%6cicenses/nebula\n" + JANE_MIT,
    "family-word-work": "polyform-noncommercial by Jane Doe\nhttps://example.org/polyform-noncommercial\n" + JANE_MIT,
    # Anything but a plain URL or an exact holder fails safe (NOTE records, not restrictions).
    "label-holder-with-license-name": "Attribution: Jane Doe, MIT License\n" + JANE_MIT,
    "label-holder-with-mail": "Author: Jane Doe <jane@example.org>\n" + JANE_MIT,
    "label-work-name": "Source: pstack\nUpstream: https://github.com/example/pstack\n" + JANE_MIT,
}

# Jin, 2026-11-02: the copyright line is never judged; whatever its name says is a NOTE record.
COPYRIGHT_NAME_NOTES = {
    "spdx-name-ban": "SPDX-License-Identifier: MIT\nCopyright 2026 X. Redistribution is prohibited.\n",
    "standard-name-ban": STANDARD_LICENSES["MIT"].replace(
        "<year> <copyright holders>", "2026 Example, REDISTRIBUTION IS PROHIBITED"),
    "name-in-brackets": STANDARD_LICENSES["MIT"].replace(
        "<year> <copyright holders>", "2026 Example (Redistribution prohibited)"),
    "two-notices": STANDARD_LICENSES["MIT"].replace(
        "<year> <copyright holders>", "2026 Example\nCopyright 2026 Other"),
    "spdx-then-copyright": "SPDX-License-Identifier: MIT\nCopyright 2026 Example Inc.\n© 2026 Other\n",
}


@pytest.mark.parametrize("text", RESTRICTED_TEXTS.values(), ids=RESTRICTED_TEXTS)
@pytest.mark.parametrize("where", ["checkout", "project-installed", "library"])
def test_a_license_with_any_added_wording_never_makes_a_default(tmp_path, text, where):
    root, worktree, library = tmp_path / "root", tmp_path / "worktree", tmp_path / "library"
    root.mkdir()
    entry = {"name": "lib", "repo": "unused", "path": str(library), "revision": "pinned", "skills": ["worded"]}
    if where == "library":
        (library / "skills/worded").mkdir(parents=True)
        (library / "skills/worded/SKILL.md").write_text("---\nname: worded\ndescription: About it\n---\nBody\n")
        (library / "skills/worded/LICENSE").write_text(text)
    else:
        base = worktree if where == "checkout" else root
        write_skill(base / ".claude", "worded", license=None)
        (base / ".claude/skills/worded/LICENSE").write_text(text)
    config = Config(skill_libraries=[entry] if where == "library" else [], default_skills={"docs": ["worded"]})
    assert "- worded: not available here" in section(config, "docs", root, worktree)
    if where != "checkout":
        with pytest.raises(SkillError, match="worded.*license"):
            SkillManager(config).check_default("worded", root)
        assert not (root / ".claude/skills/worded").exists() or where == "project-installed"


@pytest.mark.parametrize("text", [
    STANDARD_LICENSES["MIT"], "SPDX-License-Identifier: MIT\n", "  SPDX-License-Identifier:   mit  \n",
    STANDARD_LICENSES["MIT"].replace("<year> <copyright holders>", "2024-2026 Anthropic, PBC"),
    STANDARD_LICENSES["MIT"].replace("Copyright (c) <year> <copyright holders>", "Copyright 2026 Anthropic, PBC."),
    STANDARD_LICENSES["MIT"].replace("<year> <copyright holders>", "2025 Apollo.io"),
    STANDARD_LICENSES["MIT"].replace("<year> <copyright holders>", "2020- Slack Technologies, LLC"),
    STANDARD_LICENSES["MIT"].replace("<year> <copyright holders>", "2025 Zoom Video Communications, Inc."),
    STANDARD_LICENSES["MIT"].replace("\n", "  \n\n"),  # Only whitespace differs.
    STANDARD_LICENSES["ISC"].replace("<year> <owner>", "2026 Example"),
    STANDARD_LICENSES["Apache-2.0"],
    # #1652 superseded the old rejection: a permissive SPDX line beside a complete permissive text passes.
    "SPDX-License-Identifier: MIT\n" + STANDARD_LICENSES["ISC"],
    "SPDX-License-Identifier: MIT\n\n" + STANDARD_LICENSES["MIT"],
    # #1652: attribution and source notices around a complete permissive text.
    # A label value is a plain URL or exactly a name this file's copyright line writes (#1652).
    "Attribution: Jane Doe\nSource: https://example.org/example-project\n" + JANE_MIT,
    "example-project by Jane Doe, MIT License, Copyright (c) 2026 Jane Doe\n"
    "Upstream: https://example.org/example-project\n" + STANDARD_LICENSES["MIT"],
    JANE_MIT + "\ntool by Jane Doe, Apache License, Version 2.0\nAuthor: Jane Doe\nhttps://example.org/tool\n",
    "Source: https://github.com/example/pstack\nMaintainer: Jane Doe\n" + JANE_MIT,
    "Attribution: Jane van Doe\n" + STANDARD_LICENSES["MIT"].replace("<year> <copyright holders>", "2026 Jane van Doe"),
])
def test_a_plain_standard_license_still_makes_a_default(tmp_path, text):
    root = tmp_path / "root"
    write_skill(root / ".claude", "plain", license=None)
    (root / ".claude/skills/plain/LICENSE").write_text(text)
    config = Config(skill_libraries=[], default_skills={"docs": ["plain"]})
    assert ".claude/skills/plain/SKILL.md" in section(config, "docs", root)
    assert SkillManager(config).check_default("plain", root) is None


def test_an_installed_copy_without_its_own_file_needs_its_librarys_whole_license(tmp_path):
    root, library = tmp_path / "root", tmp_path / "library"
    (library / "skills/borrowed").mkdir(parents=True)
    (library / "skills/borrowed/SKILL.md").write_text("---\nname: borrowed\ndescription: About it\n---\nBody\n")
    (library / "LICENSE").write_text(RESTRICTED_TEXTS["standard-then-ban"])
    write_skill(root / ".claude", "borrowed", license=None)
    entry = {"name": "lib", "repo": "unused", "path": str(library), "revision": "pinned", "skills": ["borrowed"]}
    config = Config(skill_libraries=[entry], default_skills={"docs": ["borrowed"]})
    assert "- borrowed: not available here" in section(config, "docs", root)
    with pytest.raises(SkillError, match="borrowed.*license"):
        SkillManager(config).check_default("borrowed", root)


def test_a_linked_library_root_never_makes_a_default_but_still_lists_its_skills(tmp_path):
    real, linked = tmp_path / "real-library", tmp_path / "library"
    write_skill(real, "rooted")
    linked.symlink_to(real)
    entry = {"name": "lib", "repo": "https://github.com/anthropics/skills", "path": str(linked)}
    config = Config(skill_libraries=[entry], default_skills={"docs": ["rooted"]})
    project = tmp_path / "project"
    project.mkdir()
    assert "- rooted: not available here" in section(config, "docs", project)
    with pytest.raises(SkillError, match="symlink"):
        SkillManager(config).check_default("rooted", project)
    assert not (project / ".claude").exists()
    assert [name for name, _, _ in available_skills(Config(skill_libraries=[entry]), [])] == ["rooted"]


@pytest.mark.parametrize("linked", ["project", "checkout", "above-project"])
def test_a_linked_project_or_checkout_never_offers_its_copies_as_defaults(tmp_path, linked):
    real = tmp_path / "real"
    write_skill(real / "inner", "local")
    if linked == "above-project":
        (tmp_path / "via").symlink_to(real)
        base = tmp_path / "via/inner"
    else:
        base = tmp_path / linked
        base.symlink_to(real / "inner")
    other = tmp_path / "other"
    other.mkdir()
    root, worktree = (other, base) if linked == "checkout" else (base, other)
    config = Config(skill_libraries=[], default_skills={"docs": ["local"]})
    assert "- local: not available here" in section(config, "docs", root, worktree)


@pytest.mark.parametrize("name", ["LICENSE.md", "LICENCE", "COPYING", "license.txt", "LICENSE-THIRD-PARTY"])
@pytest.mark.parametrize("where", ["checkout", "project-installed", "library"])
def test_any_license_file_beside_the_skill_decides_before_a_parent_license(tmp_path, name, where):
    root, worktree, library = tmp_path / "root", tmp_path / "worktree", tmp_path / "library"
    root.mkdir()
    restricted = "This skill is source-available. Redistribution is prohibited.\n"
    entry = {"name": "lib", "repo": "unused", "path": str(library), "revision": "pinned", "skills": ["beside"]}
    if where == "library":
        (library / "skills/beside").mkdir(parents=True)
        (library / "skills/beside/SKILL.md").write_text("---\nname: beside\ndescription: About it\n---\nBody\n")
        (library / "LICENSE").write_text("SPDX-License-Identifier: MIT\n")
        folder = library / "skills/beside"
    else:
        base = worktree if where == "checkout" else root
        write_skill(base / ".claude", "beside", license=None)
        (base / ".claude/skills/LICENSE").write_text("SPDX-License-Identifier: MIT\n")
        folder = base / ".claude/skills/beside"
    (folder / name).write_text(restricted)
    config = Config(skill_libraries=[entry] if where == "library" else [], default_skills={"docs": ["beside"]})
    assert "- beside: not available here" in section(config, "docs", root, worktree)
    if where != "checkout":
        with pytest.raises(SkillError, match="beside.*license"):
            SkillManager(config).check_default("beside", root)
    (folder / name).write_text("SPDX-License-Identifier: MIT\n")  # The same file, permissive: it may.
    assert "beside/SKILL.md" in section(config, "docs", root, worktree)


def test_every_license_file_in_the_closest_folder_must_be_permissive(tmp_path):
    root = tmp_path / "root"
    write_skill(root / ".claude", "dual")  # LICENSE: MIT.
    (root / ".claude/skills/dual/LICENSE-APACHE").write_text(STANDARD_LICENSES["Apache-2.0"])
    config = Config(skill_libraries=[], default_skills={"docs": ["dual"]})
    assert ".claude/skills/dual/SKILL.md" in section(config, "docs", root)
    (root / ".claude/skills/dual/LICENSE-EXTRA").write_text("No redistribution.\n")
    assert "- dual: not available here" in section(config, "docs", root)


@pytest.mark.parametrize("text", COPYRIGHT_NAME_NOTES.values(), ids=COPYRIGHT_NAME_NOTES)
def test_the_copyright_name_is_never_judged(tmp_path, text):
    root = tmp_path / "root"
    write_skill(root / ".claude", "named", license=None)
    (root / ".claude/skills/named/LICENSE").write_text(text)
    config = Config(skill_libraries=[], default_skills={"docs": ["named"]})
    assert ".claude/skills/named/SKILL.md" in section(config, "docs", root)
    assert SkillManager(config).check_default("named", root) is None


@pytest.mark.parametrize("revision", ["pinned", None])
def test_an_installed_default_keeps_its_original_license_and_source(tmp_path, revision):
    library, project = tmp_path / "library", tmp_path / "project"
    project.mkdir()
    write_skill(library, "rooted", license=None)
    (library / "LICENSE").write_text(STANDARD_LICENSES["MIT"].replace("<year> <copyright holders>", "2026 Lib"))
    entry = {"name": "lib", "repo": "https://example.com/lib.git", "path": str(library)}
    if revision:
        entry.update(revision=revision, skills=["rooted"])
    manager = SkillManager(Config(skill_libraries=[entry]))
    skill = manager.check_default("rooted", project)
    manager.install_default("rooted", skill, project)
    copy = project / ".claude/skills/rooted"
    assert (copy / "LICENSE").read_text() == (library / "LICENSE").read_text()
    assert (copy / "SOURCE").read_text() == (
        "Source: https://example.com/lib.git\nPath: skills/rooted\n"
        f"Revision: {revision or 'unknown'}\nLicense: MIT (LICENSE)\n")
    config = Config(skill_libraries=[entry], default_skills={"docs": ["rooted"]})
    assert ".claude/skills/rooted/SKILL.md" in section(config, "docs", project)
    # The kept files do not make the copy a different skill for a later lane install...
    assert manager.check_install("rooted", project).path == skill.path
    # ...but a changed skill file, or any other added file, still does.
    (copy / "notes.md").write_text("mine\n")
    with pytest.raises(SkillError, match="different existing skill"):
        manager.check_install("rooted", project)
    (copy / "notes.md").unlink()
    (copy / "SKILL.md").write_text("changed\n")
    with pytest.raises(SkillError, match="different existing skill"):
        manager.check_install("rooted", project)


def test_a_default_with_its_own_license_keeps_that_file_unchanged(tmp_path):
    library, project = tmp_path / "library", tmp_path / "project"
    project.mkdir()
    write_skill(library, "own")  # LICENSE inside the skill folder: SPDX MIT.
    (library / "LICENSE").write_text(STANDARD_LICENSES["ISC"])
    entry = {"name": "lib", "repo": "unused", "path": str(library), "revision": "r1", "skills": ["own"]}
    manager = SkillManager(Config(skill_libraries=[entry]))
    manager.install_default("own", manager.check_default("own", project), project)
    copy = project / ".claude/skills/own"
    assert (copy / "LICENSE").read_text() == "SPDX-License-Identifier: MIT\n"
    assert (copy / "SOURCE").read_text().endswith("License: MIT (skills/own/LICENSE)\n")


@pytest.mark.parametrize("where", ["checkout", "project"])
@pytest.mark.parametrize("folder", [".claude/skills", "skills"])
def test_the_project_license_over_a_copy_applies_even_beside_a_same_name_mit_library(tmp_path, where, folder):
    root, worktree, library = tmp_path / "root", tmp_path / "worktree", tmp_path / "library"
    write_skill(library, "example")  # MIT in a library, under the same name.
    base = worktree if where == "checkout" else root
    write_skill(base / folder.removesuffix("skills").rstrip("/"), "example", license=None)
    (base / folder / "example/SKILL.md").write_text("---\nname: example\ndescription: Changed\n---\nMine\n")
    (base / "LICENSE").write_text("All rights reserved. Redistribution is prohibited.\n")
    root.mkdir(exist_ok=True)
    entry = {"name": "lib", "repo": "unused", "path": str(library)}
    config = Config(skill_libraries=[entry], default_skills={"docs": ["example"]})
    shown = f"({folder}/example/SKILL.md)" if where == "checkout" else str(base / folder / "example/SKILL.md")
    value = section(config, "docs", root, worktree)
    assert shown not in value, "The banned copy never reaches a session"
    assert str((library / "skills/example/SKILL.md").resolve()) in value, "The MIT original itself still may"
    if where == "project" and folder == ".claude/skills":
        with pytest.raises(SkillError, match="example.*license"):
            SkillManager(config).check_default("example", root)
    (base / "LICENSE").write_text(STANDARD_LICENSES["MIT"])  # A permissive project license: the copy may.
    assert shown in section(config, "docs", root, worktree)


@pytest.mark.parametrize("mode", [0o111, 0o444])
@pytest.mark.parametrize("where", ["checkout", "project"])
def test_a_copy_whose_license_facts_cannot_be_read_never_makes_a_default(tmp_path, mode, where):
    root, worktree, library = tmp_path / "root", tmp_path / "worktree", tmp_path / "library"
    write_skill(library, "example")  # MIT in the library.
    root.mkdir()
    base = worktree if where == "checkout" else root
    write_skill(base / ".claude", "example", license="LicenseRef-No-Redistribution")
    folder = base / ".claude/skills/example"
    entry = {"name": "lib", "repo": "unused", "path": str(library)}
    config = Config(skill_libraries=[entry], default_skills={"docs": ["example"]})
    folder.chmod(mode)
    try:
        value = section(config, "docs", root, worktree)
        assert ".claude/skills/example/SKILL.md" not in value.replace(str(library), "")
        if where == "project":
            with pytest.raises(SkillError, match="example"):
                SkillManager(config).check_default("example", root)
    finally:
        folder.chmod(0o755)


def test_an_unreadable_folder_inside_a_skill_counts_as_a_link(tmp_path):
    from foragent.skills import reached_through_symlink
    write_skill(tmp_path, "example")
    hidden = tmp_path / "skills/example/inner"
    hidden.mkdir()
    assert not reached_through_symlink(tmp_path / "skills/example")
    hidden.chmod(0o000)
    try:
        assert reached_through_symlink(tmp_path / "skills/example")
    finally:
        hidden.chmod(0o755)


def test_a_license_entry_that_is_not_a_readable_file_is_unknown(tmp_path):
    from foragent.skills import redistributable_license
    write_skill(tmp_path, "example", license=None)
    folder = tmp_path / "skills/example"
    (folder / "LICENSES").mkdir()
    (folder / "LICENSES/MIT.txt").write_text(STANDARD_LICENSES["MIT"])
    assert redistributable_license(folder, tmp_path)[0] == "unknown"


NOTICED_TEXTS = {
    "attribution-prefix": "Attribution: Jane Doe\nSource: https://example.org/noticed\n" + JANE_MIT,
    "attribution-suffix": JANE_MIT + "\nnoticed by Jane Doe, MIT License\nhttps://example.org/noticed\n",
    "spdx-and-holder": "SPDX-License-Identifier: MIT\nAttribution: Jane Doe\n" + JANE_MIT,
}


@pytest.mark.parametrize("text", NOTICED_TEXTS.values(), ids=NOTICED_TEXTS)
@pytest.mark.parametrize("where", ["library", "repository"])
def test_a_license_the_default_judge_accepts_installs_without_another_veto_or_warning(tmp_path, capsys, text, where):
    """#1652: the section, the install check and the install all follow one license judge."""
    library, project = tmp_path / "library", tmp_path / "project"
    project.mkdir()
    base = library if where == "library" else project
    write_skill(base, "noticed", license=None)
    (base / "skills/noticed/LICENSE").write_text(text)
    entry = {"name": "lib", "repo": "https://example.com/lib.git", "path": str(library), "revision": "pinned",
             "skills": ["noticed"]}
    manager = SkillManager(Config(skill_libraries=[entry] if where == "library" else []))
    skill = manager.check_default("noticed", project)
    assert skill is not None and skill.license == "MIT"
    manager.install_default("noticed", skill, project)
    assert "installing locally only" not in capsys.readouterr().err
    copy = project / ".claude/skills/noticed"
    assert (copy / "LICENSE").read_text() == text
    config = Config(skill_libraries=[entry] if where == "library" else [], default_skills={"docs": ["noticed"]})
    assert ".claude/skills/noticed/SKILL.md" in section(config, "docs", project)
