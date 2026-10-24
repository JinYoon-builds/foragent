from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
import yaml

from foragent.config import Config, ConfigError, DEFAULTS, in_run_hours, load_config, resolved_reviewer


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".foragent.yml").write_text("{}\n", encoding="utf-8")
    return root


def write_settings(path, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")


def test_exact_defaults(repo):
    expected = {
        "review": "batch", "review_paths": {}, "always_human": [], "executor": "codex",
        "executor_model": None, "reviewer": None, "reviewer_model": None, "review_max": 2, "approvers": [],
        "acceptance_dir": "tests/acceptance/", "notify": None, "notify_summary_hour": 8, "run_hours": "always",
        "stall_minutes": 30, "max_runtime_minutes": 90, "max_retries": 2, "conflict_retries": 2, "base_branch": "main",
        "orchestrator_timeout_minutes": 30,
        "tick_failure_alert": 3,
        "repo": None, "board": None, "board_map": None, "language": "en", "codex_goal": False, "review_level": "low",
        "review_scope": "claude-local", "test_command": None, "runner_unit_tests": "auto",
        "interview": "native", "ouroboros_version": None, "profile": None,
        "skill_roles": {},
        "lanes": {}, "max_parallel": None, "parallel_floor": 1, "test_slots": 2, "test_slots_floor": 1,
        "executor_fallback_model": None,
        "transient_max_wait_minutes": 30, "limit_wait_minutes": 60,
        "tiers": {"s": {"codex_effort": "medium", "claude_model": None},
                  "m": {"codex_effort": "high", "claude_model": None},
                  "l": {"codex_effort": "xhigh", "claude_model": None}},
        "skill_libraries": [{"name": "knowledge-work-plugins",
                             "repo": "https://github.com/anthropics/knowledge-work-plugins"},
                            {"name": "skills", "repo": "https://github.com/anthropics/skills"}],
    }
    assert DEFAULTS == expected
    assert asdict(load_config(repo)) == expected


def test_tiers_merge_global_and_repository_fields(repo, tmp_path):
    global_path = tmp_path / "global.yml"
    write_settings(global_path, {"tiers": {"s": {"claude_model": "small"}, "m": {"codex_effort": "xhigh"}}})
    write_settings(repo / ".foragent.yml", {"tiers": {"s": {"codex_effort": "low"}}})
    config = load_config(repo, global_path)
    assert config.tiers["s"] == {"claude_model": "small", "codex_effort": "low"}
    assert config.tiers["m"]["codex_effort"] == "xhigh"
    assert config.tiers["l"] == DEFAULTS["tiers"]["l"]


@pytest.mark.parametrize("value", [[], {"xl": {}}, {"s": None}, {"s": {"codex_effort": []}},
                                    {"m": {"claude_model": 3}}, {"l": {"other": True}}])
def test_invalid_tiers_are_rejected(repo, value):
    write_settings(repo / ".foragent.yml", {"tiers": value})
    with pytest.raises(ConfigError, match="tiers"):
        load_config(repo)


def test_merge_order_and_nested_mapping_replacement(repo, tmp_path):
    global_path = tmp_path / "global.yml"
    write_settings(global_path, {
        "review": "careful", "executor": "claude", "executor_model": "global-model",
        "review_paths": {"global/**": "careful"}, "approvers": ["global-user"],
    })
    write_settings(repo / ".foragent.yml", {
        "review": "fast", "review_paths": {"local/**": "batch"},
        "approvers": ["local-user"], "executor_model": None,
    })
    config = load_config(repo, global_path)
    assert config.review == "fast"
    assert config.executor == "claude"
    assert config.executor_model is None
    assert config.review_paths == {"local/**": "batch"}
    assert config.approvers == ["local-user"]
    assert config.max_retries == 2


def test_default_global_location(repo, monkeypatch, tmp_path):
    home = tmp_path / "custom-home"
    monkeypatch.setenv("HOME", str(home))
    write_settings(home / ".config/foragent/config.yml", {"base_branch": "trunk"})
    assert load_config(repo).base_branch == "trunk"


def test_missing_global_and_empty_yaml_are_allowed(repo, tmp_path):
    (repo / ".foragent.yml").write_text("", encoding="utf-8")
    assert asdict(load_config(repo, tmp_path / "missing.yml")) == DEFAULTS


@pytest.mark.parametrize("roles", [[], None, {"pack:demo": "pretty"}, {"pack:demo": []},
                                   {"pack:demo": None}, {1: "style"}, {"": "style"}])
def test_invalid_skill_roles_are_rejected(repo, roles):
    write_settings(repo / ".foragent.yml", {"skill_roles": roles})
    with pytest.raises(ConfigError, match="skill_roles"):
        load_config(repo)


def test_missing_repository_config(tmp_path):
    with pytest.raises(ConfigError, match=r"^not a Foragent repo: \.foragent.yml missing$"):
        load_config(tmp_path)


@pytest.mark.parametrize("source", ["local", "global"])
def test_unknown_keys_are_rejected(repo, tmp_path, source):
    global_path = tmp_path / "global.yml"
    path = repo / ".foragent.yml" if source == "local" else global_path
    write_settings(path, {"unknown_option": True})
    with pytest.raises(ConfigError, match="unknown_option"):
        load_config(repo, global_path)


@pytest.mark.parametrize("text", ["[a, b]", "scalar", "false", "review: ["])
def test_invalid_yaml_and_non_mapping_config(repo, text):
    (repo / ".foragent.yml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(repo)


@pytest.mark.parametrize("key,value", [
    ("orchestrator_timeout_minutes", 0), ("orchestrator_timeout_minutes", -1),
    ("orchestrator_timeout_minutes", True), ("orchestrator_timeout_minutes", "5"),
    ("interview", "magic"), ("interview", None), ("interview", []), ("interview", True),
    ("ouroboros_version", 52), ("ouroboros_version", []), ("ouroboros_version", False),
    ("review", "FAST"), ("review", None), ("review", []),
    ("review_paths", []), ("review_paths", {"app/**": "unknown"}),
    ("review_paths", {1: "fast"}), ("review_paths", {"app/**": []}),
    ("executor", "other"), ("executor", None), ("executor", []),
    ("reviewer", "other"), ("reviewer", []),
    ("codex_goal", "true"), ("codex_goal", 1), ("codex_goal", None),
    ("review_level", "extreme"), ("review_level", []), ("review_level", None),
    ("review_scope", "unknown"), ("review_scope", []), ("review_scope", None),
    ("test_command", ""), ("test_command", "  "), ("test_command", []), ("test_command", True),
    ("always_human", "auth/**"), ("always_human", [1]),
    ("approvers", "user"), ("approvers", [None]),
    ("executor_model", 42), ("reviewer_model", []),
    ("acceptance_dir", ""), ("acceptance_dir", None), ("base_branch", "  "),
    ("run_hours", "24-08"), ("run_hours", "09-24"), ("run_hours", "9-18"),
    ("run_hours", "09:00-18:00"), ("run_hours", "Always"), ("run_hours", None),
    ("run_hours", 1), ("run_hours", "09-18\n"),
    ("repo", "owner"), ("repo", "owner/repo/extra"), ("repo", " owner/repo"),
    ("repo", ""), ("repo", 3), ("repo", []),
])
def test_validation_errors_name_the_field(repo, key, value):
    write_settings(repo / ".foragent.yml", {key: value})
    with pytest.raises(ConfigError, match=key):
        load_config(repo)


def test_interview_settings_merge_global_and_repository(repo, tmp_path):
    global_path = tmp_path / "global.yml"
    write_settings(global_path, {"interview": "native", "ouroboros_version": "0.51.1"})
    config = load_config(repo, global_path)
    assert config.interview == "native" and config.ouroboros_version == "0.51.1"
    write_settings(repo / ".foragent.yml", {"interview": "ouroboros", "ouroboros_version": None})
    config = load_config(repo, global_path)
    assert config.interview == "ouroboros" and config.ouroboros_version is None


@pytest.mark.parametrize("source", ["local", "global"])
@pytest.mark.parametrize("profile", ["daily-app", [], False, 3])
def test_profile_requires_mapping_or_null(repo, tmp_path, source, profile):
    global_path = tmp_path / "global.yml"
    path = repo / ".foragent.yml" if source == "local" else global_path
    write_settings(path, {"profile": profile})
    with pytest.raises(ConfigError, match="profile"):
        load_config(repo, global_path)


def test_profile_uses_local_mapping_and_can_be_cleared(repo, tmp_path):
    global_path = tmp_path / "global.yml"
    write_settings(global_path, {"profile": {"kind": "daily-app", "stage": "build"}})
    assert load_config(repo, global_path).profile == {"kind": "daily-app", "stage": "build"}
    write_settings(repo / ".foragent.yml", {"profile": {"kind": "tool"}})
    assert load_config(repo, global_path).profile == {"kind": "tool"}
    write_settings(repo / ".foragent.yml", {"profile": None})
    assert load_config(repo, global_path).profile is None


@pytest.mark.parametrize("source", ["local", "global"])
@pytest.mark.parametrize("directory", [
    "tests/acceptance[1]/", "tests/acceptance]/", "checks*/", "checks?/",
    "checks{old,new}/", "checks}/",
])
def test_acceptance_directory_rejects_glob_metacharacters(repo, tmp_path, source, directory):
    global_path = tmp_path / "global.yml"
    path = repo / ".foragent.yml" if source == "local" else global_path
    write_settings(path, {"acceptance_dir": directory})
    with pytest.raises(ConfigError, match="acceptance_dir.*glob metacharacters"):
        load_config(repo, global_path)


@pytest.mark.parametrize("directory", ["tests/acceptance/", "checks with spaces/", "./checks/"])
def test_acceptance_directory_allows_literal_paths(repo, directory):
    write_settings(repo / ".foragent.yml", {"acceptance_dir": directory})
    assert load_config(repo).acceptance_dir == directory


@pytest.mark.parametrize("key", ["stall_minutes", "max_runtime_minutes", "max_retries", "conflict_retries",
                                 "review_max", "test_slots_floor", "parallel_floor"])
@pytest.mark.parametrize("value", [0, -1, 1.5, "2", True, None])
def test_integer_fields_are_positive_integers(repo, key, value):
    write_settings(repo / ".foragent.yml", {key: value})
    with pytest.raises(ConfigError, match=key):
        load_config(repo)


@pytest.mark.parametrize("source", ["local", "global"])
def test_test_slots_floor_cannot_exceed_ceiling(repo, tmp_path, source):
    global_path = tmp_path / "global.yml"
    path = repo / ".foragent.yml" if source == "local" else global_path
    write_settings(path, {"test_slots": 2, "test_slots_floor": 3})
    with pytest.raises(ConfigError, match="test_slots_floor.*test_slots"):
        load_config(repo, global_path)


def test_test_slots_floor_uses_merged_settings(repo, tmp_path):
    global_path = tmp_path / "global.yml"
    write_settings(global_path, {"test_slots": 3, "test_slots_floor": 2})
    assert load_config(repo, global_path).test_slots_floor == 2
    write_settings(repo / ".foragent.yml", {"test_slots_floor": 1})
    assert load_config(repo, global_path).test_slots_floor == 1
    write_settings(repo / ".foragent.yml", {"test_slots": 1})
    with pytest.raises(ConfigError, match="test_slots_floor.*test_slots"):
        load_config(repo, global_path)


@pytest.mark.parametrize("source", ["local", "global"])
def test_parallel_floor_cannot_exceed_project_ceiling(repo, tmp_path, source):
    global_path = tmp_path / "global.yml"
    path = repo / ".foragent.yml" if source == "local" else global_path
    write_settings(path, {"max_parallel": 2, "parallel_floor": 3})
    with pytest.raises(ConfigError, match="parallel_floor.*max_parallel"):
        load_config(repo, global_path)


@pytest.mark.parametrize("settings", [
    {"parallel_floor": 3}, {"parallel_floor": 3, "max_parallel": 3},
    {"parallel_floor": 1, "max_parallel": 1},
])
def test_parallel_floor_accepts_no_ceiling_or_a_floor_equal_to_ceiling(repo, settings):
    write_settings(repo / ".foragent.yml", settings)
    config = load_config(repo)
    assert config.parallel_floor == settings["parallel_floor"]
    assert config.max_parallel == settings.get("max_parallel")


def test_parallel_floor_uses_merged_settings(repo, tmp_path):
    global_path = tmp_path / "global.yml"
    write_settings(global_path, {"parallel_floor": 3})
    assert load_config(repo, global_path).parallel_floor == 3
    write_settings(repo / ".foragent.yml", {"max_parallel": 2})
    with pytest.raises(ConfigError, match="parallel_floor.*max_parallel"):
        load_config(repo, global_path)
    write_settings(repo / ".foragent.yml", {"parallel_floor": 1, "max_parallel": 2})
    assert load_config(repo, global_path).parallel_floor == 1


def test_all_valid_overrides(repo):
    settings = {
        "review": "careful", "review_paths": {"app/**": "fast", "lib/**": "batch"},
        "always_human": ["auth/**"], "executor": "claude", "reviewer": "codex",
        "executor_model": "implementation-model", "reviewer_model": "review-model",
        "approvers": ["human"], "acceptance_dir": "checks/", "notify": ["slack:#project"],
        "run_hours": "22-08", "stall_minutes": 1, "max_runtime_minutes": 1,
        "max_retries": 1, "conflict_retries": 3, "review_max": 3, "base_branch": "trunk",
        "repo": "owner/name", "language": "ko", "codex_goal": True, "review_level": "max",
        "review_scope": "pr-body",
    }
    write_settings(repo / ".foragent.yml", settings)
    assert asdict(load_config(repo)) == DEFAULTS | settings


def test_language_uses_global_default_and_repo_override(repo, tmp_path):
    global_path = tmp_path / "global.yml"
    write_settings(global_path, {"language": "ko"})
    assert load_config(repo, global_path).language == "ko"
    write_settings(repo / ".foragent.yml", {"language": "ja"})
    assert load_config(repo, global_path).language == "ja"


@pytest.mark.parametrize("language", ["ko", "en", "ja", "en-US", "zh-Hant-TW", "es-419"])
def test_language_tags(repo, language):
    write_settings(repo / ".foragent.yml", {"language": language})
    assert load_config(repo).language == language


@pytest.mark.parametrize("language", [None, "", "ko KR", "en_US", 1, [], "a" * 40])
def test_invalid_language(repo, language):
    write_settings(repo / ".foragent.yml", {"language": language})
    with pytest.raises(ConfigError, match="language"):
        load_config(repo)


def test_configs_do_not_share_mutable_defaults(repo):
    first = load_config(repo)
    first.review_paths["app/**"] = "fast"
    first.approvers.append("human")
    assert load_config(repo).review_paths == {}
    assert load_config(repo).approvers == []
    assert DEFAULTS["review_paths"] == {}
    assert DEFAULTS["approvers"] == []


@pytest.mark.parametrize("executor,reviewer,expected", [
    ("codex", None, "claude"), ("claude", None, "codex"),
    ("codex", "codex", "codex"), ("claude", "claude", "claude"),
    ("codex", "claude", "claude"),
    ("codex", "none", "none"),
])
def test_resolved_reviewer(executor, reviewer, expected):
    assert resolved_reviewer(replace(Config(), executor=executor, reviewer=reviewer)) == expected


def test_disabled_reviewer_loads(repo):
    write_settings(repo / ".foragent.yml", {"reviewer": "none"})
    assert resolved_reviewer(load_config(repo)) == "none"


@pytest.mark.parametrize("hours,hour,minute,expected", [
    ("always", 0, 0, True), ("always", 23, 59, True),
    ("09-18", 8, 59, False), ("09-18", 9, 0, True),
    ("09-18", 17, 59, True), ("09-18", 18, 0, False),
    ("22-08", 21, 59, False), ("22-08", 22, 0, True),
    ("22-08", 23, 59, True), ("22-08", 0, 0, True),
    ("22-08", 7, 59, True), ("22-08", 8, 0, False),
    ("00-00", 23, 59, True), ("09-09", 8, 0, True),
    ("00-23", 0, 0, True), ("00-23", 23, 0, False),
    ("23-00", 23, 0, True), ("23-00", 0, 0, False),
])
def test_run_hours_boundaries(hours, hour, minute, expected):
    assert in_run_hours(datetime(2026, 10, 20, hour, minute, tzinfo=UTC), hours) is expected


def test_run_hours_uses_supplied_local_hour():
    local = datetime(2026, 10, 20, 9, tzinfo=timezone(timedelta(hours=9)))
    assert in_run_hours(local, "09-18")


def test_invalid_run_hours_raises():
    with pytest.raises(ValueError, match="run_hours"):
        in_run_hours(datetime.now(UTC), "24-08")


@pytest.mark.parametrize("settings", [
    {"lanes": []}, {"lanes": {"Upper": {}}}, {"lanes": {"a_b": {}}},
    {"lanes": {"": {}}}, {"lanes": {1: {}}}, {"lanes": {"ui": None}},
    {"lanes": {"ui": {"paths": "src/**"}}}, {"lanes": {"ui": {"paths": [1]}}},
    {"lanes": {"ui": {"agent": 1}}}, {"lanes": {"ui": {"model": []}}},
    {"lanes": {"ui": {"executor": "other"}}}, {"lanes": {"ui": {"unknown": True}}},
])
def test_lane_validation(repo, settings):
    write_settings(repo / ".foragent.yml", settings)
    with pytest.raises(ConfigError, match="lanes"):
        load_config(repo)


@pytest.mark.parametrize("key", ["max_parallel", "transient_max_wait_minutes", "limit_wait_minutes"])
@pytest.mark.parametrize("value", [0, -1, 1.5, "2", True, None])
def test_parallel_and_wait_settings_are_positive(repo, key, value):
    write_settings(repo / ".foragent.yml", {key: value})
    with pytest.raises(ConfigError, match=key):
        load_config(repo)


def test_lane_overrides_and_nulls_load(repo):
    lanes = {"ui-2": {"paths": ["src/**"], "agent": "fa-ui", "executor": "claude", "model": "lane-model"},
             "data": {"paths": [], "agent": None, "executor": None, "model": None}}
    write_settings(repo / ".foragent.yml", {"lanes": lanes, "max_parallel": 1, "executor_fallback_model": "fallback"})
    config = load_config(repo)
    assert config.lanes == lanes and config.max_parallel == 1
    assert config.executor_fallback_model == "fallback"
