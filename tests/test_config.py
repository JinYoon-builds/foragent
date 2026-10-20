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
        "executor_model": None, "reviewer": None, "reviewer_model": None, "approvers": [],
        "acceptance_dir": "tests/acceptance/", "notify": None, "run_hours": "always",
        "stall_minutes": 30, "max_runtime_minutes": 90, "max_retries": 2, "base_branch": "main",
    }
    assert DEFAULTS == expected
    assert asdict(load_config(repo)) == expected


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
    ("review", "FAST"), ("review", None), ("review", []),
    ("review_paths", []), ("review_paths", {"app/**": "unknown"}),
    ("review_paths", {1: "fast"}), ("review_paths", {"app/**": []}),
    ("executor", "other"), ("executor", None), ("executor", []),
    ("reviewer", "other"), ("reviewer", []),
    ("always_human", "auth/**"), ("always_human", [1]),
    ("approvers", "user"), ("approvers", [None]),
    ("executor_model", 42), ("reviewer_model", []),
    ("acceptance_dir", ""), ("acceptance_dir", None), ("base_branch", "  "),
    ("run_hours", "24-08"), ("run_hours", "09-24"), ("run_hours", "9-18"),
    ("run_hours", "09:00-18:00"), ("run_hours", "Always"), ("run_hours", None),
    ("run_hours", 1), ("run_hours", "09-18\n"),
])
def test_validation_errors_name_the_field(repo, key, value):
    write_settings(repo / ".foragent.yml", {key: value})
    with pytest.raises(ConfigError, match=key):
        load_config(repo)


@pytest.mark.parametrize("key", ["stall_minutes", "max_runtime_minutes", "max_retries"])
@pytest.mark.parametrize("value", [0, -1, 1.5, "2", True, None])
def test_integer_fields_are_positive_integers(repo, key, value):
    write_settings(repo / ".foragent.yml", {key: value})
    with pytest.raises(ConfigError, match=key):
        load_config(repo)


def test_all_valid_overrides(repo):
    settings = {
        "review": "careful", "review_paths": {"app/**": "fast", "lib/**": "batch"},
        "always_human": ["auth/**"], "executor": "claude", "reviewer": "codex",
        "executor_model": "implementation-model", "reviewer_model": "review-model",
        "approvers": ["human"], "acceptance_dir": "checks/", "notify": ["slack:#project"],
        "run_hours": "22-08", "stall_minutes": 1, "max_runtime_minutes": 1,
        "max_retries": 1, "base_branch": "trunk",
    }
    write_settings(repo / ".foragent.yml", settings)
    assert asdict(load_config(repo)) == settings


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
])
def test_resolved_reviewer(executor, reviewer, expected):
    assert resolved_reviewer(replace(Config(), executor=executor, reviewer=reviewer)) == expected


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
