"""Unit tests for `fa settings` (#160)."""

import json
from pathlib import Path

import pytest
import yaml

from foragent import cli, machine_budget, settings
from foragent.scheduler_start import _memory_holds


@pytest.fixture
def root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".foragent.yml").write_text("repo: owner/repo  # the repo\nskill_libraries: []\n")
    monkeypatch.chdir(project)
    return project


def run(root, *args):
    try:
        return cli.main(["settings", *args, "--repo-root", str(root)])
    except SystemExit as exc:
        return exc.code


def out(root, capsys, *args):
    capsys.readouterr()
    assert run(root, *args) == 0
    return capsys.readouterr().out


def test_list_has_exactly_the_eleven_keys_in_scope_order(root, capsys):
    items = json.loads(out(root, capsys, "list", "--json"))
    assert [(item["scope"], item["key"]) for item in items] == [
        ("team", "review"), ("team", "always_human"), ("team", "executor"), ("team", "reviewer"),
        ("me", "claude_quota.mode"), ("me", "notify"), ("me", "animation"), ("me", "language"),
        ("me", "view_glyphs"), ("machine", "max_parallel"), ("machine", "memory_pressure_limit"), ("machine", "log_retention_days")]
    values = {item["key"]: item["value"] for item in items}
    assert values["memory_pressure_limit"] == "critical" and values["log_retention_days"] == 7


def test_plain_list_and_get_print_scope_key_value_description(root, capsys):
    lines = out(root, capsys, "list").splitlines()
    assert len(lines) == 12
    assert lines[0] == "team  review  batch  " + settings.item(settings.BY_KEY["review"], root, "en")["description"]
    assert out(root, capsys, "get", "always_human") == "-\n"
    assert run(root, "set", "always_human", "docs/**, LICENSE") == 0
    assert out(root, capsys, "get", "always_human") == "docs/**,LICENSE\n"


def test_descriptions_follow_the_configured_language(root, capsys):
    (root / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\nlanguage: ko\n")
    english = settings.item(settings.BY_KEY["review"], root, "en")["description"]
    korean = json.loads(out(root, capsys, "get", "review", "--json"))["description"]
    assert korean and korean != english


def test_team_set_keeps_comments_and_unset_restores_the_default(root, capsys):
    team = root / ".foragent.yml"
    assert run(root, "set", "reviewer", "claude") == 0
    assert "# the repo" in team.read_text() and yaml.safe_load(team.read_text())["reviewer"] == "claude"
    assert run(root, "set", "reviewer", "codex") == 0
    assert team.read_text().count("reviewer") == 1
    assert run(root, "set", "reviewer", "--unset") == 0
    assert "reviewer" not in yaml.safe_load(team.read_text())
    assert "# the repo" in team.read_text()
    assert json.loads(out(root, capsys, "get", "reviewer", "--json"))["value"] is None


def test_executor_refuses_when_the_repository_lists_executors(root, capsys):
    team = root / ".foragent.yml"
    team.write_text("repo: owner/repo\nskill_libraries: []\nexecutors:\n  - name: claude\n    quota: subscription\n"
                    "    keep_percent: 80\n")
    before = team.read_bytes()
    capsys.readouterr()
    assert run(root, "set", "executor", "codex") != 0
    assert "executors" in capsys.readouterr().err
    assert team.read_bytes() == before


def test_notify_takes_fixed_values_and_patterns_as_written(root, capsys):
    user = Path.home() / ".config/foragent/config.yml"
    for value in ("macos", "command:/bin/true", "ntfy:topic", "slack:https://x"):
        assert run(root, "set", "notify", value) == 0
        assert yaml.safe_load(user.read_text())["notify"] == value
    for value in ("email", "slack:", "command:"):
        assert run(root, "set", "notify", value) != 0
    assert yaml.safe_load(user.read_text())["notify"] == "slack:https://x"


def test_me_scope_unset_drops_the_key_and_keeps_other_user_settings(root, capsys):
    user = Path.home() / ".config/foragent/config.yml"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text("skill_libraries: []\nclaude_quota:\n  keep_percent: 70\n")
    assert run(root, "set", "claude_quota.mode", "burn") == 0
    assert yaml.safe_load(user.read_text())["claude_quota"] == {"keep_percent": 70, "mode": "burn"}
    assert run(root, "set", "claude_quota.mode", "--unset") == 0
    assert yaml.safe_load(user.read_text()) == {"skill_libraries": [], "claude_quota": {"keep_percent": 70}}
    assert run(root, "set", "animation", "false") == 0
    assert out(root, capsys, "get", "animation") == "false\n"


@pytest.mark.parametrize("key,value", [("log_retention_days", "0"), ("log_retention_days", "366"),
                                       ("max_parallel", "0"), ("memory_pressure_limit", "normal"),
                                       ("language", "ja"), ("review", "")])
def test_bad_values_write_nothing(root, key, value):
    assert run(root, "set", key, value) != 0
    assert not settings.machine_file().exists()
    assert yaml.safe_load((root / ".foragent.yml").read_text()) == {"repo": "owner/repo", "skill_libraries": []}


def test_machine_values_live_in_one_shared_state_file(root, capsys, tmp_path, monkeypatch):
    assert run(root, "set", "log_retention_days", "30") == 0
    assert run(root, "set", "memory_pressure_limit", "warn") == 0
    assert settings.machine_file() == Path.home() / ".local/state/foragent/machine-settings.yml"
    assert yaml.safe_load(settings.machine_file().read_text()) == {
        "memory_pressure_limit": "warn", "log_retention_days": 30}
    other = tmp_path / "other"
    other.mkdir()
    (other / ".foragent.yml").write_text("repo: owner/other\n")
    assert out(other, capsys, "get", "log_retention_days") == "30\n"
    assert run(root, "set", "log_retention_days", "--unset") == 0
    assert out(other, capsys, "get", "log_retention_days") == "7\n"


def test_machine_max_parallel_wins_over_machine_yml_with_source_settings(root):
    path = machine_budget.machine_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("max_parallel: 6\ntest_slots: 3\n")
    assert machine_budget.read().report()["max_parallel"] == {"value": 6, "source": "machine"}
    assert run(root, "set", "max_parallel", "2") == 0
    report = machine_budget.read().report()
    assert report["max_parallel"] == {"value": 2, "source": "settings"}
    assert report["test_slots"] == {"value": 3, "source": "machine"}
    assert run(root, "set", "max_parallel", "--unset") == 0
    assert machine_budget.read().report()["max_parallel"] == {"value": 6, "source": "machine"}


def test_memory_pressure_limit_decides_which_pressure_holds_new_starts(root):
    assert [_memory_holds(level) for level in ("normal", "warn", "critical")] == [False, False, True]
    assert run(root, "set", "memory_pressure_limit", "warn") == 0
    assert [_memory_holds(level) for level in ("normal", "warn", "critical")] == [False, True, True]


@pytest.mark.parametrize("where", ["team", "me"])
def test_misplaced_machine_keys_are_ignored_by_list_get_and_set(root, capsys, where):
    misplaced = "memory_pressure_limit: warn\nlog_retention_days: 9\n"
    if where == "team":
        team = root / ".foragent.yml"
        team.write_text(team.read_text() + misplaced)
    else:
        user = Path.home() / ".config/foragent/config.yml"
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text("skill_libraries: []\n" + misplaced)
    items = {item["key"]: item["value"] for item in json.loads(out(root, capsys, "list", "--json"))}
    assert items["memory_pressure_limit"] == "critical" and items["log_retention_days"] == 7
    assert out(root, capsys, "get", "log_retention_days") == "7\n"
    for key, value in (("review", "careful"), ("language", "ko"), ("max_parallel", "2")):
        assert run(root, "set", key, value) == 0
        assert settings.get_item(root, key, "en")["value"] == settings.parse(settings.BY_KEY[key], value, "en")


def test_a_problem_in_another_setting_does_not_block_a_change(root, capsys):
    user = Path.home() / ".config/foragent/config.yml"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text("skill_libraries: []\nnotify_summary_hour: 99\n")
    assert run(root, "set", "review", "careful") == 0
    assert run(root, "set", "animation", "false") == 0
    assert yaml.safe_load(user.read_text())["animation"] is False


def test_a_change_that_itself_breaks_loading_is_refused_and_an_old_problem_is_not():
    from foragent.config import ConfigError
    from foragent.settings_write import _check

    good, broken = {"review": "batch"}, {"review": "batch", "notify_summary_hour": 99}
    with pytest.raises(ConfigError, match="notify_summary_hour"):
        _check(({}, good), ({}, broken), "review", "batch", "en")
    _check(({}, broken), ({}, {**broken, "review": "careful"}), "review", "careful", "en")
    _check(({}, good), ({}, {**good, "log_retention_days": 3}), "review", "batch", "en")


@pytest.mark.parametrize("key,lines", [
    ("reviewer", ["reviewer: claude # chosen reviewer\n"]),
    ("review", ["review: careful  # slow on purpose\n"]),
    ("executor", ["# who writes\n", "executor: claude # implementer\n"]),
    ("always_human", ["always_human: # people check these\n", "  - docs/** # docs\n", "  - LICENSE\n"]),
])
def test_unset_keeps_every_comment_and_restores_the_default(root, capsys, key, lines):
    team = root / ".foragent.yml"
    team.write_text("repo: owner/repo  # the repo\n" + "".join(lines) + "skill_libraries: []\n")
    comments = [line[line.index("#"):] for line in team.read_text().splitlines() if "#" in line]
    assert run(root, "set", key, "--unset") == 0
    text = team.read_text()
    assert key not in yaml.safe_load(text)
    assert [line[line.index("#"):] for line in text.splitlines() if "#" in line] == comments
    assert yaml.safe_load(text) == {"repo": "owner/repo", "skill_libraries": []}
    assert settings.get_item(root, key, "en")["value"] == settings.BY_KEY[key].default


TEAM_VALUES = {"review": ("careful", "careful"), "always_human": ("docs/**,LICENSE", ["docs/**", "LICENSE"]),
               "executor": ("claude", "claude"), "reviewer": ("none", "none")}


@pytest.mark.parametrize("key", list(TEAM_VALUES))
@pytest.mark.parametrize("layout", ["{%s, repo: owner/repo, skill_libraries: []}",
                                    "{repo: owner/repo, %s, skill_libraries: []}",
                                    "{repo: owner/repo, skill_libraries: [], %s}"])
def test_flow_style_team_file_sets_and_unsets_every_team_key(root, key, layout):
    team = root / ".foragent.yml"
    raw, stored = TEAM_VALUES[key]
    pair = f"{key}: {yaml.safe_dump(stored, default_flow_style=True).strip()}"
    team.write_text(layout.replace("%s", pair) + "  # flow\n")
    assert run(root, "set", key, "--unset") == 0
    assert yaml.safe_load(team.read_text()) == {"repo": "owner/repo", "skill_libraries": []}
    assert team.read_text().endswith("}  # flow\n")
    assert settings.get_item(root, key, "en")["value"] == settings.BY_KEY[key].default
    assert run(root, "set", key, raw) == 0
    assert yaml.safe_load(team.read_text())[key] == stored
    assert run(root, "set", key, raw) == 0
    assert team.read_text().count(key) == 1


@pytest.mark.parametrize("old", ["burn", "[keep]", "null", "7"])
def test_claude_quota_mode_is_stored_whatever_claude_quota_held(root, capsys, old):
    user = Path.home() / ".config/foragent/config.yml"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(f"skill_libraries: []\nclaude_quota: {old}\n")
    assert run(root, "set", "claude_quota.mode", "burn") == 0
    assert yaml.safe_load(user.read_text())["claude_quota"] == {"mode": "burn"}
    assert out(root, capsys, "get", "claude_quota.mode") == "burn\n"
    assert run(root, "set", "claude_quota.mode", "--unset") == 0
    assert yaml.safe_load(user.read_text()) == {"skill_libraries": []}
    assert out(root, capsys, "get", "claude_quota.mode") == "keep\n"


def test_a_block_value_is_replaced_with_its_comments_kept(root, capsys):
    team = root / ".foragent.yml"
    team.write_text("repo: owner/repo\nreview:  # wrong shape\n  level: careful  # old\nskill_libraries: []\n")
    assert run(root, "set", "review", "fast") == 0
    assert yaml.safe_load(team.read_text()) == {"repo": "owner/repo", "review": "fast", "skill_libraries": []}
    assert "# wrong shape" in team.read_text() and "# old" in team.read_text()


def test_a_write_that_would_not_read_back_is_refused_before_writing(root, monkeypatch, capsys):
    from foragent import settings_write

    user = Path.home() / ".config/foragent/config.yml"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text("skill_libraries: []\n")
    before = user.read_bytes()
    monkeypatch.setattr(settings_write, "value_in", lambda setting, data: "keep")
    capsys.readouterr()
    assert run(root, "set", "claude_quota.mode", "burn") != 0
    assert "keep" in capsys.readouterr().err and user.read_bytes() == before
    team_before = (root / ".foragent.yml").read_bytes()
    assert run(root, "set", "review", "careful") != 0
    assert (root / ".foragent.yml").read_bytes() == team_before
    monkeypatch.setattr(settings_write, "machine_values", lambda data: {})
    assert run(root, "set", "log_retention_days", "9") != 0
    assert not settings.machine_file().exists()
