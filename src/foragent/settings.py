"""`fa settings`: thirteen settings in three scopes, each read from and written to its own file (#160).

- team: the repository's ``.foragent.yml``
- me: the user config, ``~/.config/foragent/config.yml``
- machine: ``$XDG_STATE_HOME/foragent/machine-settings.yml``, shared by every repository;
  this module is its only writer.

Values show what each scope's own file says (or the default); nothing here merges scopes.
settings_write changes them.
No LLM or network is involved.
"""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from foragent import config as config_module
from foragent.config import ConfigError
from foragent.i18n import text

MACHINE_FILE = "machine-settings.yml"
NOTIFY_FIXED = ("none", "claude-push", "macos")
NOTIFY_PATTERNS = ("command:<path>", "slack:<x>", "ntfy:<x>")
PRESSURE_LEVELS = ("warn", "critical")


@dataclass(frozen=True)
class Setting:
    key: str
    scope: str  # team, me or machine
    default: Any
    choices: tuple[Any, ...] | None = None
    kind: str = "choice"  # choice, list, notify, bool, positive, range
    low: int | None = None
    high: int | None = None


SETTINGS = (
    Setting("review", "team", "batch", ("fast", "batch", "careful")),
    Setting("always_human", "team", [], kind="list"),
    Setting("executor", "team", "codex", ("claude", "codex")),
    Setting("reviewer", "team", None, ("claude", "codex", "none")),
    Setting("runner_unit_tests", "team", "auto", ("auto", "ci", "all")),
    Setting("orch_silent_alert_minutes", "team", 30, kind="range", low=0, high=1440),
    Setting("claude_quota.mode", "me", "keep", ("keep", "burn")),
    Setting("notify", "me", None, NOTIFY_FIXED, kind="notify"),
    Setting("animation", "me", True, (True, False), kind="bool"),
    Setting("language", "me", "en", ("en", "ko")),
    Setting("view_glyphs", "me", "auto", ("auto", "emoji", "ascii")),
    Setting("max_parallel", "machine", None, kind="positive"),
    Setting("memory_pressure_limit", "machine", "critical", PRESSURE_LEVELS),
    Setting("log_retention_days", "machine", 7, kind="range", low=1, high=365),
)
BY_KEY = {setting.key: setting for setting in SETTINGS}
MACHINE_KEYS = tuple(setting.key for setting in SETTINGS if setting.scope == "machine")
# Machine keys with no Config field: found in .foragent.yml or the user config, fa settings ignores them.
MACHINE_ONLY = frozenset(key for key in MACHINE_KEYS if key not in config_module.DEFAULTS)


def language(root: Path) -> str:
    """The configured language (repository first, then user), read without validating anything else."""
    for path in (_team_path(root), config_module._global_path()):
        try:
            value = _yaml_file(path, "en").get("language")
        except ConfigError:
            continue
        if isinstance(value, str) and value:
            return value
    return "en"


def machine_file() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(state_home) / "foragent" / MACHINE_FILE


def _setting(key: str, language: str) -> Setting:
    if key not in BY_KEY:
        raise ConfigError(text("settings_unknown_key", language, name=key, keys=", ".join(BY_KEY)))
    return BY_KEY[key]


def parse(setting: Setting, raw: str, language: str) -> Any:
    """Turn the command-line text into the stored value, or raise one localized line."""
    key = setting.key
    if setting.kind == "list":
        return [part.strip() for part in raw.split(",") if part.strip()]
    if setting.kind == "bool":
        if raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        raise ConfigError(text("settings_bad_choice", language, name=key, choices="true, false", value=raw))
    if setting.kind in ("positive", "range"):
        try:
            number = int(raw.strip())
        except ValueError:
            number = None
        if setting.kind == "positive" and (number is None or number < 1):
            raise ConfigError(text("settings_bad_positive", language, name=key, value=raw))
        if setting.kind == "range" and (number is None or not setting.low <= number <= setting.high):
            raise ConfigError(text("settings_bad_number", language, name=key, low=setting.low,
                                   high=setting.high, value=raw))
        return number
    if raw in setting.choices:
        return raw
    if setting.kind == "notify":
        prefix, _, rest = raw.partition(":")
        if prefix in ("command", "slack", "ntfy") and rest:
            return raw
    choices = ", ".join([*setting.choices, *(NOTIFY_PATTERNS if setting.kind == "notify" else ())])
    raise ConfigError(text("settings_bad_choice", language, name=key, choices=choices, value=raw))


# Reading each scope's own file.

def _team_path(root: Path) -> Path:
    return root / ".foragent.yml"


def _yaml_file(path: Path, language: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(text("settings_file_invalid", language, path=path, error=exc)) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(text("settings_file_invalid", language, path=path, error="not a mapping"))
    return data


def read_machine() -> dict[str, Any]:
    """The machine file's valid values; a missing, bad or partly bad file never raises."""
    try:
        data = yaml.safe_load(machine_file().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return machine_values(data)


def machine_values(data: Any) -> dict[str, Any]:
    """The valid machine values in parsed machine-file data."""
    if not isinstance(data, dict):
        return {}
    found = {}
    for key in MACHINE_KEYS:
        value = data.get(key)
        # Only what `fa settings set` writes counts: an int or a string, never a YAML bool.
        if type(value) not in (int, str):
            continue
        try:
            found[key] = parse(BY_KEY[key], str(value), "en")
        except ConfigError:
            continue
    return found


def value_in(setting: Setting, data: dict[str, Any]) -> Any:
    """The value `get` shows for a team or me setting, from that scope's parsed file."""
    if setting.key == "executor" and "executor" not in data:
        entries = data.get("executors")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict) and entries[0].get("name"):
            return entries[0]["name"]
    if setting.key == "claude_quota.mode":
        quota = data.get("claude_quota")
        return quota.get("mode", setting.default) if isinstance(quota, dict) else setting.default
    value = data.get(setting.key, setting.default)
    return deepcopy(value) if value is not None else setting.default


def _file_value(setting: Setting, root: Path, language: str) -> Any:
    if setting.scope == "machine":
        return read_machine().get(setting.key, setting.default)
    path = _team_path(root) if setting.scope == "team" else config_module._global_path()
    return value_in(setting, _yaml_file(path, language))


def item(setting: Setting, root: Path, language: str) -> dict[str, Any]:
    result = {"key": setting.key, "scope": setting.scope, "value": _file_value(setting, root, language),
              "default": setting.default,
              "description": text(f"settings_desc_{setting.key}", language)}
    if setting.choices is not None:
        result["choices"] = list(setting.choices)
    if setting.kind == "notify":
        result["patterns"] = list(NOTIFY_PATTERNS)
    if setting.kind == "range":
        result["range"] = [setting.low, setting.high]
    return result


def list_items(root: Path, language: str) -> list[dict[str, Any]]:
    return [item(setting, root, language) for setting in SETTINGS]


def get_item(root: Path, key: str, language: str) -> dict[str, Any]:
    return item(_setting(key, language), root, language)


def plain(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(part) for part in value) or "-"
    return str(value)


def raw(value: Any) -> str:
    """The command-line text that `fa settings set` parses back into value."""
    return plain(value) if isinstance(value, bool) else str(value)


def effective_value(setting: Setting, config: Any, store: Any = None) -> Any:
    """What applies now: the loaded configuration (team over me), the machine file and,
    for the Claude quota mode, the repository's local override (fa quota mode)."""
    if setting.key == "claude_quota.mode":
        if store is not None:
            from foragent import scheduler_quota

            return scheduler_quota.quota_mode(store, config)[0]
        return config.claude_quota.get("mode", setting.default)
    if setting.scope == "machine":
        return read_machine().get(setting.key, getattr(config, setting.key, setting.default))
    return deepcopy(getattr(config, setting.key, setting.default))


def effective(root: Path, store: Any = None, language: str = "en") -> dict[str, Any]:
    """Every setting, nested by its dotted key (claude_quota.mode under claude_quota).

    Team, me and machine values come from each scope's own file; the repository's local
    override (fa quota mode) decides the Claude quota mode when it is set.
    """
    result: dict[str, Any] = {}
    for setting in SETTINGS:
        value = _file_value(setting, root, language)
        if setting.key == "claude_quota.mode" and store is not None:
            value = store.claude_quota_mode() or value
        *parents, name = setting.key.split(".")
        target = result
        for parent in parents:
            target = target.setdefault(parent, {})
        target[name] = value
    return result
