"""Load repository settings over optional global settings."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import re
from typing import Any

import yaml


class ConfigError(ValueError):
    """Configuration could not be loaded or validated."""


@dataclass(frozen=True)
class Config:
    review: str = "batch"
    review_paths: dict[str, str] = field(default_factory=dict)
    always_human: list[str] = field(default_factory=list)
    executor: str = "codex"
    executor_model: str | None = None
    codex_goal: bool = False
    reviewer: str | None = None
    reviewer_model: str | None = None
    review_max: int = 2
    review_level: str = "medium"
    approvers: list[str] = field(default_factory=list)
    acceptance_dir: str = "tests/acceptance/"
    notify: Any = None
    run_hours: str = "always"
    stall_minutes: int = 30
    max_runtime_minutes: int = 90
    max_retries: int = 2
    base_branch: str = "main"
    repo: str | None = None
    language: str = "en"


DEFAULTS: dict[str, Any] = asdict(Config())
_REVIEW_LEVELS = {"fast", "batch", "careful"}
_EXECUTORS = {"codex", "claude"}


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"configuration must be a mapping: {path}")
    for key in value:
        if key not in DEFAULTS:
            raise ConfigError(f"unknown configuration key: {key}")
    return value


def _hours(run_hours: str) -> tuple[int, int] | None:
    if run_hours == "always":
        return None
    if not isinstance(run_hours, str) or not re.fullmatch(
        r"(?:[01][0-9]|2[0-3])-(?:[01][0-9]|2[0-3])", run_hours
    ):
        raise ValueError("run_hours must be 'always' or HH-HH (00-23)")
    start, end = run_hours.split("-")
    return int(start), int(end)


def _validate(values: dict[str, Any]) -> None:
    if type(values["codex_goal"]) is not bool:
        raise ConfigError("codex_goal must be a boolean")
    if not isinstance(values["review_level"], str) or values["review_level"] not in {
        "low", "medium", "high", "xhigh", "max",
    }:
        raise ConfigError("review_level must be low, medium, high, xhigh, or max")
    language = values["language"]
    if not isinstance(language, str) or len(language) > 35 or not re.fullmatch(
        r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language
    ):
        raise ConfigError("language must be a short language tag, such as en, ko, or ja")
    if not isinstance(values["review"], str) or values["review"] not in _REVIEW_LEVELS:
        raise ConfigError("review must be fast, batch, or careful")
    paths = values["review_paths"]
    if not isinstance(paths, dict):
        raise ConfigError("review_paths must be a mapping")
    for path, review in paths.items():
        if not isinstance(path, str) or not isinstance(review, str) or review not in _REVIEW_LEVELS:
            raise ConfigError(f"invalid review_paths entry: {path}")
    for key in ("executor", "reviewer"):
        value = values[key]
        if key == "reviewer" and value is None:
            continue
        allowed = _EXECUTORS | {"none"} if key == "reviewer" else _EXECUTORS
        if not isinstance(value, str) or value not in allowed:
            raise ConfigError(f"{key} must be codex or claude" + (" or none" if key == "reviewer" else ""))
    for key in ("stall_minutes", "max_runtime_minutes", "max_retries", "review_max"):
        if type(values[key]) is not int or values[key] <= 0:
            raise ConfigError(f"{key} must be a positive integer")
    for key in ("always_human", "approvers"):
        if not isinstance(values[key], list) or any(not isinstance(item, str) for item in values[key]):
            raise ConfigError(f"{key} must be a list of strings")
    for key in ("executor_model", "reviewer_model"):
        if values[key] is not None and not isinstance(values[key], str):
            raise ConfigError(f"{key} must be a string or null")
    for key in ("acceptance_dir", "base_branch"):
        if not isinstance(values[key], str) or not values[key].strip():
            raise ConfigError(f"{key} must be a nonempty string")
    if any(char in values["acceptance_dir"] for char in "[]*?{}"):
        raise ConfigError("acceptance_dir must not contain glob metacharacters: [ ] * ? { }")
    repo = values["repo"]
    if repo is not None and (
        not isinstance(repo, str) or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None
    ):
        raise ConfigError("repo must be owner/name or null")
    try:
        _hours(values["run_hours"])
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def load_config(repo_root: Path, global_path: Path | None = None) -> Config:
    local_path = repo_root / ".foragent.yml"
    if not local_path.is_file():
        raise ConfigError("not a Foragent repo: .foragent.yml missing")
    if global_path is None:
        global_path = Path.home() / ".config/foragent/config.yml"
    values = deepcopy(DEFAULTS)
    if global_path.exists():
        values.update(_read_yaml(global_path))
    values.update(_read_yaml(local_path))
    _validate(values)
    return Config(**values)


def resolved_reviewer(config: Config) -> str:
    return config.reviewer or ("claude" if config.executor == "codex" else "codex")


def in_run_hours(now: datetime, run_hours: str) -> bool:
    hours = _hours(run_hours)
    if hours is None:
        return True
    start, end = hours
    if start == end:
        return True
    if start < end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end
