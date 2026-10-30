"""Acceptance test for skills-add-partial-pack. Locked by Foragent; do not edit."""

import json

import yaml

from test_skills_add import skill_pack
from test_skills_autosync import cli_exit, isolated_home, write_global_config


BROKEN_PATH = "skills/review-broken/SKILL.md"
INVALID_SKILL = "---\nname: review-broken\ndescription: Review compatibility: check agents\n---\nReview.\n"


def partial_pack(git_history, tmp_path):
    # The broken entry sorts between polish and tone, so discovery must continue.
    git_history.update({BROKEN_PATH: INVALID_SKILL})
    return skill_pack(git_history, tmp_path)


def test_skills_add_partial_pack_json_lists_valid_skills_and_one_line_skip(
    git_history, tmp_path, capsys,
):
    from foragent.cli import main

    source = partial_pack(git_history, tmp_path)
    config = write_global_config({"skill_libraries": []})
    before = config.read_bytes()
    code = cli_exit(main, ["skills", "add", str(source), "--json"])
    output = capsys.readouterr()
    assert code == 0, output.err or output.out
    payload = json.loads(output.out)
    assert {skill["name"] for skill in payload["skills"]} == {"polish", "tone"}
    assert len(payload["skills"]) == 2
    skipped, = payload["skipped"]
    assert set(skipped) == {"path", "error"}
    assert skipped["path"] == BROKEN_PATH
    assert isinstance(skipped["error"], str) and skipped["error"].strip()
    assert "\n" not in skipped["error"] and "\r" not in skipped["error"]
    assert config.read_bytes() == before


def test_skills_add_partial_pack_approval_registers_only_valid_skills_and_reports_skip(
    git_history, tmp_path, capsys,
):
    from foragent.cli import main

    source = partial_pack(git_history, tmp_path)
    config = write_global_config({"skill_libraries": []})
    roles = {"skill-pack:polish": "review", "skill-pack:tone": "review"}
    code = cli_exit(main, ["skills", "add", str(source), "--approve",
                           "--role", "skill-pack:polish=review", "--role", "skill-pack:tone=review"])
    output = capsys.readouterr()
    assert code == 0, output.err or output.out
    assert BROKEN_PATH in output.out + output.err
    saved = yaml.safe_load(config.read_text())
    assert any(entry["repo"] == str(source) for entry in saved["skill_libraries"])
    assert saved["skill_roles"] == roles
    code = cli_exit(main, ["skills", "catalog", "--json"])
    output = capsys.readouterr()
    assert code == 0, output.err or output.out
    # #1028 (2026-10-31): the catalog may also list other libraries (vendored principles); check this pack's rows.
    catalog = [skill for skill in json.loads(output.out) if skill["id"].startswith("skill-pack:")]
    assert len(catalog) == 2
    assert {skill["name"] for skill in catalog} == {"polish", "tone"}
    assert {skill["id"]: skill["role"] for skill in catalog} == roles


def test_skills_add_all_invalid_names_every_path_and_registers_nothing(git_history, capsys):
    from foragent.cli import main

    invalid_paths = ("skills/alpha-broken/SKILL.md", "skills/zeta-broken/SKILL.md")
    git_history.update({path: INVALID_SKILL for path in invalid_paths})
    config = write_global_config({"skill_libraries": [], "language": "ko"})
    before = config.read_bytes()
    # #1028 (2026-10-31): other libraries (vendored principles) may be listed; registering nothing
    # means the catalog is unchanged by this add.
    assert cli_exit(main, ["skills", "catalog", "--json"]) == 0
    catalog_before = json.loads(capsys.readouterr().out)
    code = cli_exit(main, ["skills", "add", str(git_history.origin), "--approve"])
    output = capsys.readouterr()
    message = output.out + output.err
    assert code != 0, message
    assert config.read_bytes() == before
    for path in invalid_paths:
        assert path in message, f"all-invalid error omitted {path}: {message}"
    assert cli_exit(main, ["skills", "catalog", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == catalog_before
