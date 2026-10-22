"""Acceptance test for settings-cmd. Locked by Foragent; do not edit."""

from pathlib import Path
import json

import yaml

from test_order_progress import offline_local_cli
from test_skills_autosync import cli_exit, isolated_home


def settings_command(case, *args):
    return cli_exit(case.main, ["settings", *args, "--repo-root", str(case.root)])


def file_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def setting_value(case, key, capsys):
    capsys.readouterr()
    assert settings_command(case, "get", key, "--json") == 0
    result = json.loads(capsys.readouterr().out)
    # The spec fixes list's item schema, but permits scalar or item-shaped get JSON.
    return result["value"] if isinstance(result, dict) else result


def test_settings_cmd_list_describes_all_scopes_and_fixed_choices_without_llm(tmp_path, monkeypatch, capsys):
    case = offline_local_cli(tmp_path, monkeypatch)
    assert settings_command(case, "list", "--json") == 0
    items = json.loads(capsys.readouterr().out)
    assert isinstance(items, list)
    assert len({item["key"] for item in items}) == len(items)
    by_key = {item["key"]: item for item in items}
    scopes = {"team": ("review", "always_human", "executor", "reviewer"),
              "me": ("claude_quota.mode", "notify", "animation", "language"),
              "machine": ("max_parallel", "memory_pressure_limit", "log_retention_days")}
    for scope, keys in scopes.items():
        for key in keys:
            item = by_key[key]
            assert {"key", "scope", "value", "description"} <= item.keys()
            assert item["scope"] == scope
            assert isinstance(item["description"], str) and item["description"].strip()
    for key, choices in {"review": {"fast", "batch", "careful"},
                         "claude_quota.mode": {"keep", "burn"}, "animation": {True, False}}.items():
        assert set(by_key[key]["choices"]) == choices
    assert case.gh_calls == []


def test_settings_cmd_set_persists_only_the_owning_scope_and_get_reads_it(tmp_path, monkeypatch, capsys):
    case = offline_local_cli(tmp_path, monkeypatch)
    team = case.root / ".foragent.yml"
    user = Path.home() / ".config/foragent/config.yml"
    user_before = user.read_bytes()
    machine_before = file_bytes(case.state_home)
    assert settings_command(case, "set", "review", "careful") == 0
    assert yaml.safe_load(team.read_text())["review"] == "careful"
    assert user.read_bytes() == user_before and file_bytes(case.state_home) == machine_before
    assert setting_value(case, "review", capsys) == "careful"
    team_before = team.read_bytes()
    assert settings_command(case, "set", "claude_quota.mode", "burn") == 0
    assert yaml.safe_load(user.read_text())["claude_quota"]["mode"] == "burn"
    assert yaml.safe_load(user.read_text())["skill_libraries"] == []
    assert team.read_bytes() == team_before and file_bytes(case.state_home) == machine_before
    assert setting_value(case, "claude_quota.mode", capsys) == "burn"
    user_before = user.read_bytes()
    assert settings_command(case, "set", "max_parallel", "3") == 0
    assert team.read_bytes() == team_before and user.read_bytes() == user_before
    changed = {path for path, data in file_bytes(case.state_home).items() if machine_before.get(path) != data}
    # Lock files are plumbing; exactly one persistent machine-settings file changes.
    settings_files = [case.state_home / path for path in changed if not path.endswith(".lock")]
    assert len(settings_files) == 1
    assert case.state_home in settings_files[0].parents
    assert settings_files[0].read_bytes(), "The machine value must be persisted, not cached in memory"
    assert setting_value(case, "max_parallel", capsys) == 3
    assert case.gh_calls == []


def test_settings_cmd_invalid_value_is_atomic_and_does_not_invoke_executor(tmp_path, monkeypatch, capsys):
    case = offline_local_cli(tmp_path, monkeypatch)
    # Establish a valid command first: an absent parser is not evidence of validation.
    assert settings_command(case, "set", "review", "batch") == 0
    before = file_bytes(tmp_path)
    assert settings_command(case, "set", "review", "fastest") != 0
    assert file_bytes(tmp_path) == before
    assert setting_value(case, "review", capsys) == "batch"
    assert case.gh_calls == []
