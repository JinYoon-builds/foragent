"""`fa settings set`: change one setting in the file its scope owns, validated first (#160)."""

from __future__ import annotations

import fcntl
import re
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from foragent import config as config_module
from foragent.config import ConfigError, LaneSettings
from foragent.config_validation import _validate
from foragent.i18n import text
from foragent.settings import (
    BY_KEY,
    MACHINE_KEYS,
    MACHINE_ONLY,
    Setting,
    _setting,
    _team_path,
    _yaml_file,
    item,
    machine_file,
    machine_values,
    parse,
    plain,
    read_machine,
    value_in,
)


def set_value(root: Path, key: str, raw: str | None, *, unset: bool, language: str) -> dict[str, Any]:
    setting = _setting(key, language)
    if unset and raw is not None:
        raise ConfigError(text("settings_value_and_unset", language, name=key))
    if not unset and raw is None:
        raise ConfigError(text("settings_value_missing", language, name=key))
    value = None if unset else parse(setting, raw, language)
    if setting.scope == "machine":
        _set_machine(key, value, unset)
    elif setting.scope == "team":
        _set_team(root, setting, value, unset, raw, language)
    else:
        _set_me(root, setting, value, unset, raw, language)
    return item(setting, root, language)


def _load_error(global_values: dict[str, Any], local: dict[str, Any]) -> Exception | None:
    """Why the configuration would not load as load_config reads it, or None when it loads.

    Machine keys that are not Config keys are ignored here, as fa settings ignores them elsewhere.
    """
    values = deepcopy(config_module.DEFAULTS)
    try:
        sources = [{name: value for name, value in source.items() if name not in MACHINE_ONLY}
                   for source in (global_values, local)]
        for source in sources:
            for name in source:
                if (name not in config_module.DEFAULTS and name not in config_module.IGNORED_KEYS
                        and name not in config_module.FILE_KEYS):
                    raise ConfigError(f"unknown configuration key: {name}")
        config_module._overlay(values, sources[0])
        config_module._overlay(values, sources[1])
        config_module._apply_executors(values, *sources)
        _validate(values)
    except (ConfigError, TypeError, ValueError) as exc:
        return exc
    return None


def _check(before: tuple[dict[str, Any], dict[str, Any]], after: tuple[dict[str, Any], dict[str, Any]],
           key: str, raw: Any, language: str) -> None:
    """Refuse a change only when it is what stops the configuration from loading.

    The value already passed its fixed choices; a problem elsewhere that was there
    before this change belongs to that other setting, so it never blocks this one.
    """
    error = _load_error(*after)
    if error is not None and _load_error(*before) is None:
        raise ConfigError(text("settings_rejected", language, name=key, value=plain(raw), error=error))


def _other_file(path: Path) -> dict[str, Any]:
    """Another scope's file, read only to check cross-setting rules; unreadable counts as empty."""
    try:
        return _yaml_file(path, "en")
    except ConfigError:
        return {}


def _saved(setting: Setting, found: Any, expected: Any, path: Path, language: str) -> None:
    """Refuse, before writing, a change whose file would not read back as the requested value."""
    if found != expected:
        raise ConfigError(text("settings_not_saved", language, path=path, name=setting.key,
                               found=plain(found), value=plain(expected)))


def _remove_flow_pair(edit: LaneSettings, mapping: yaml.MappingNode, found: tuple[yaml.Node, yaml.Node]) -> None:
    """Delete `key: value` from a `{...}` mapping with the comma that joins it to a neighbour."""
    pairs = mapping.value
    index = next(number for number, pair in enumerate(pairs) if pair[0] is found[0])
    if index > 0:
        start, end = pairs[index - 1][1].end_mark.index, found[1].end_mark.index  # `, key: value`
    elif len(pairs) > 1:
        start, end = found[0].start_mark.index, pairs[1][0].start_mark.index  # `key: value, `
    else:
        start, end = found[0].start_mark.index, found[1].end_mark.index
    text_ = "".join(edit.lines)
    text_ = text_[:start] + text_[end:]
    edit.lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z", text_)


def _remove_pair(edit: LaneSettings, found: tuple[yaml.Node, yaml.Node]) -> None:
    """Delete a top-level key's lines, leaving each of their comments behind on its own line."""
    column = found[0].start_mark.column
    start, end = found[0].start_mark.line, config_module._last_line(*found)
    kept = [" " * column + comment for line in edit.lines[start:end + 1]
            for comment in config_module._comments([line])]
    edit.lines[start:end + 1] = kept


def _set_team(root: Path, setting: Setting, value: Any, unset: bool, raw: Any, language: str) -> None:
    path = _team_path(root)
    local = _yaml_file(path, language)
    if setting.key == "executor" and ("executors" in local or local.get("executor_list") is not None):
        raise ConfigError(text("settings_executor_list", language))
    changed = {k: v for k, v in local.items() if not (unset and k == setting.key)}
    if not unset:
        changed[setting.key] = value
    user = _other_file(config_module._global_path())
    _check((user, local), (user, changed), setting.key, raw, language)
    expected = setting.default if unset else value
    _saved(setting, value_in(setting, changed), expected, path, language)
    edit = LaneSettings(path)
    edit.expected = changed
    found = config_module._pair(edit.root, setting.key)
    line = f"{setting.key}: {config_module._flow(value)}{edit.newline}"
    flow_root = edit.root is not None and edit.root.flow_style
    if unset:
        if found is not None and flow_root:
            _remove_flow_pair(edit, edit.root, found)
        elif found is not None:
            _remove_pair(edit, found)
    elif setting.kind == "list":
        edit.set_list(setting.key, value)
    elif found is None and flow_root:
        edit._flow_add(edit.root, setting.key, value)
    elif found is None and edit.root is not None:
        edit._insert(config_module._last_line(*edit.root.value[-1]) + 1, [line],
                     before=edit.root.value[0][0].start_mark.line)
    elif found is None:
        edit._insert(len(edit.lines), [line], before=0)
    elif config_module._empty(found[1]):
        edit._after_colon(found[0], config_module._flow(value))
    elif isinstance(found[1], yaml.ScalarNode) or found[1].flow_style:
        edit._replace(found[1], config_module._flow(value))
    elif not flow_root:
        # A block value (only an invalid one here): drop its lines, keeping comments, and write the new one.
        start = found[0].start_mark.line
        _remove_pair(edit, found)
        edit.lines[start:start] = [" " * found[0].start_mark.column + line]
    else:
        raise ConfigError(text("settings_file_layout", language, path=path, name=setting.key))
    try:
        content = edit.check()
    except ConfigError as exc:
        raise ConfigError(text("settings_file_layout", language, path=path, name=setting.key)) from exc
    path.write_bytes(content.encode("utf-8"))


def _set_me(root: Path, setting: Setting, value: Any, unset: bool, raw: Any, language: str) -> None:
    path = config_module._global_path()
    values = _yaml_file(path, language)
    before = deepcopy(values)
    if setting.key == "claude_quota.mode":
        quota = values.get("claude_quota")
        # A non-mapping claude_quota cannot hold a mode; this setting replaces it.
        quota = {k: v for k, v in quota.items() if k != "mode"} if isinstance(quota, dict) else {}
        if not unset:
            quota["mode"] = value
        if quota:
            values["claude_quota"] = quota
        else:
            values.pop("claude_quota", None)
    elif unset:
        values.pop(setting.key, None)
    else:
        values[setting.key] = value
    _saved(setting, value_in(setting, values), setting.default if unset else value, path, language)
    team = _other_file(_team_path(root))
    _check((before, team), (values, team), setting.key, raw, language)
    config_module._write_global(path, values)


@contextmanager
def _machine_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _set_machine(key: str, value: Any, unset: bool) -> None:
    path = machine_file()
    with _machine_lock(path):
        values = read_machine()
        values.pop(key, None)
        if not unset:
            values[key] = value
        content = yaml.safe_dump({k: values[k] for k in MACHINE_KEYS if k in values}, sort_keys=False)
        expected = BY_KEY[key].default if unset else value
        _saved(BY_KEY[key], machine_values(yaml.safe_load(content)).get(key, BY_KEY[key].default),
               expected, path, "en")
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
