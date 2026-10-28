"""Validate merged repository settings before constructing Config."""

import re
from typing import Any

from foragent import review_perspectives


def _validate(values: dict[str, Any]) -> None:
    from foragent.config import ConfigError, SKILL_ROLES, _EXECUTORS, _REVIEW_LEVELS, _hours

    if type(values["notify_summary_hour"]) is not int or not 0 <= values["notify_summary_hour"] <= 23:
        raise ConfigError("notify_summary_hour must be an integer from 0 to 23")
    board_map = values["board_map"]
    if board_map is not None and (not isinstance(board_map, dict) or any(
        key not in {"Backlog", "Up next", "In progress", "Your turn", "Done"}
        or not isinstance(value, str) or not value.strip() for key, value in board_map.items()
    )):
        raise ConfigError("board_map must map board groups to nonempty column names or be null")
    if values["profile"] is not None and not isinstance(values["profile"], dict):
        raise ConfigError("profile must be a mapping or null")
    if values["test_command"] is not None and (
        not isinstance(values["test_command"], str) or not values["test_command"].strip()
    ):
        raise ConfigError("test_command must be a nonempty string or null")
    if not isinstance(values["runner_unit_tests"], str) or values["runner_unit_tests"] not in {"auto", "ci", "all"}:
        raise ConfigError("runner_unit_tests must be auto, ci, or all")
    if not isinstance(values["checks"], str) or values["checks"] not in {"local", "ci"}:
        raise ConfigError("checks must be local or ci")
    if values["checks_fallback"] is not None and values["checks_fallback"] != "local":
        raise ConfigError("checks_fallback must be local or null")
    if type(values["ci_shards"]) is not int or not 1 <= values["ci_shards"] <= 256:
        raise ConfigError("ci_shards must be an integer from 1 to 256")
    labels = values["ci_runs_on"]
    if isinstance(labels, str):
        labels = [labels]
    if not isinstance(labels, list) or not labels or any(not isinstance(label, str) or not label.strip() for label in labels):
        raise ConfigError("ci_runs_on must be a nonempty string or nonempty list of strings")
    if not isinstance(values["review_scope"], str) or values["review_scope"] not in {"claude-local", "pr-body"}:
        raise ConfigError("review_scope must be claude-local or pr-body")
    if not isinstance(values["interview"], str) or values["interview"] not in {"ouroboros", "native"}:
        raise ConfigError("interview must be ouroboros or native")
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
    listed = _validate_executors(values.get("executors"))
    for key in ("executor", "reviewer"):
        value = values[key]
        if key == "reviewer" and value is None:
            continue
        allowed = _EXECUTORS | {"none"} if key == "reviewer" else _EXECUTORS | listed
        if not isinstance(value, str) or value not in allowed:
            raise ConfigError(f"{key} must be codex or claude" + (" or none" if key == "reviewer" else ""))
    for key in ("stall_minutes", "max_runtime_minutes", "max_retries", "conflict_retries", "review_max",
                "max_parallel", "overlap_per_file", "transient_max_wait_minutes", "limit_wait_minutes",
                "usage_recheck_minutes", "orchestrator_timeout_minutes", "tick_failure_alert", "test_slots"):
        if key == "max_parallel" and values[key] is None:
            continue
        if type(values[key]) is not int or values[key] <= 0:
            raise ConfigError(f"{key} must be a positive integer")
    if type(values["merge_turn_size"]) is not int or values["merge_turn_size"] != 1:
        raise ConfigError("merge_turn_size must be 1 until merge groups (#459)")
    review_perspectives.validate(values, ConfigError)
    from foragent.order_perspectives import SEATS
    seats = values["perspectives"]
    if not isinstance(seats, dict) or any(seat not in SEATS or not isinstance(executor, str) or executor not in _EXECUTORS
                                          for seat, executor in seats.items()):
        from foragent.i18n import text
        language = values.get("language") if isinstance(values.get("language"), str) else "en"
        raise ConfigError(text("perspectives_config_shape", language, seats=", ".join(SEATS)))
    if not isinstance(values["view_glyphs"], str) or values["view_glyphs"] not in {"emoji", "ascii", "auto"}:
        raise ConfigError("view_glyphs must be emoji, ascii, or auto")
    if type(values["animation"]) is not bool:
        raise ConfigError("animation must be true or false")
    if type(values["update_check"]) is not bool:
        raise ConfigError("update_check must be true or false")
    presence = values["presence"]
    if not isinstance(presence, dict):
        raise ConfigError("presence must be a mapping of statusline, session_start and report")
    from foragent.config import PRESENCE_DEFAULTS
    for key, value in presence.items():
        if key not in PRESENCE_DEFAULTS:
            raise ConfigError(f"unknown presence setting: {key} (use statusline, session_start or report)")
        if type(value) is not bool:
            raise ConfigError(f"presence.{key} must be true or false")
    if values["upstream_reports"] is not None and type(values["upstream_reports"]) is not bool:
        from foragent.i18n import text
        language = values["language"] if isinstance(values["language"], str) else "en"
        raise ConfigError(text("config_upstream_reports_invalid", language))
    for key in ("always_human", "approvers"):
        if not isinstance(values[key], list) or any(not isinstance(item, str) for item in values[key]):
            raise ConfigError(f"{key} must be a list of strings")
    for key in ("executor_model", "reviewer_model", "executor_fallback_model", "ouroboros_version", "board"):
        if values[key] is not None and not isinstance(values[key], str):
            raise ConfigError(f"{key} must be a string or null")
    lanes = values["lanes"]
    if not isinstance(lanes, dict):
        raise ConfigError("lanes must be a mapping")
    for name, lane in lanes.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]+", name):
            raise ConfigError(f"invalid lanes name: {name}")
        if not isinstance(lane, dict) or lane.keys() - {"paths", "agent", "skills", "executor", "reviewer", "model", "title", "emoji"}:
            raise ConfigError(f"invalid lanes entry: {name}")
        paths = lane.get("paths", [])
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ConfigError(f"lanes.{name}.paths must be a list of strings")
        if lane.get("reviewer") is not None and lane["reviewer"] not in _EXECUTORS | {"none"}:
            raise ConfigError(f"lanes.{name}.reviewer must be codex, claude, none, or null")
        for key in ("agent", "executor", "reviewer", "model", "title", "emoji"):
            if lane.get(key) is not None and not isinstance(lane[key], str):
                raise ConfigError(f"lanes.{name}.{key} must be a string or null")
        if lane.get("executor") is not None and lane["executor"] not in _EXECUTORS | listed:
            raise ConfigError(f"lanes.{name}.executor must be codex, claude, an executors name, or null")
        if not isinstance(lane.get("skills", []), list) or any(
            not isinstance(skill, str) for skill in lane.get("skills", [])
        ):
            raise ConfigError(f"lanes.{name}.skills must be a list of strings")
    _validate_roles(values["roles"])
    quota = values["claude_quota"]
    if not isinstance(quota, dict):
        raise ConfigError("claude_quota must be a mapping with mode and keep_percent")
    for key in quota.keys() - {"mode", "keep_percent"}:
        raise ConfigError(f"claude_quota.{key} is not a known key; use mode or keep_percent")
    if quota.get("mode") not in ("keep", "burn"):
        raise ConfigError("claude_quota.mode must be keep or burn")
    if type(quota.get("keep_percent")) is not int or not 1 <= quota["keep_percent"] <= 100:
        raise ConfigError("claude_quota.keep_percent must be an integer from 1 to 100")
    tiers = values["tiers"]
    if not isinstance(tiers, dict) or set(tiers) != {"s", "m", "l"}:
        raise ConfigError("tiers must define s, m, and l")
    for name, tier in tiers.items():
        if not isinstance(tier, dict) or set(tier) != {"codex_effort", "claude_model"}:
            raise ConfigError(f"tiers.{name} must contain codex_effort and claude_model")
        if tier["codex_effort"] not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError(f"tiers.{name}.codex_effort is invalid")
        if tier["claude_model"] is not None and not isinstance(tier["claude_model"], str):
            raise ConfigError(f"tiers.{name}.claude_model must be a string or null")
    libraries = values["skill_libraries"]
    if not isinstance(libraries, list):
        raise ConfigError("skill_libraries must be a list of repository URLs or library mappings")
    for library in libraries:
        if isinstance(library, str) and library.strip():
            continue
        if (not isinstance(library, dict) or set(library) - {"name", "repo", "path"}
                or not all(isinstance(library.get(key), str) and library[key].strip() for key in ("name", "repo"))
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", library["name"])
                or library["name"] in {".", ".."}
                or ("path" in library and not isinstance(library["path"], str))):
            raise ConfigError("invalid skill_libraries entry; expected name, repo, and optional path")
    roles = values["skill_roles"]
    if not isinstance(roles, dict) or any(
        not isinstance(identity, str) or not identity.strip()
        or not isinstance(role, str) or role not in SKILL_ROLES
        for identity, role in roles.items()
    ):
        raise ConfigError("skill_roles must map skill IDs to style, review, tool, or domain")
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


QUOTAS = ("subscription", "api", "unlimited")


def _validate_executors(entries: Any) -> set[str]:
    """Check an explicit ordered executors list (#786); return its names."""
    from foragent.config import ConfigError, DerivedExecutors

    if isinstance(entries, DerivedExecutors):
        return set()
    # The default is always a derived list, so None here is an explicit `executors: null`.
    if not isinstance(entries, list) or not entries:
        raise ConfigError("executors must be a nonempty list of {name, quota} entries")
    names: list[str] = []
    for index, entry in enumerate(entries):
        where = f"executors[{index}]"
        if not isinstance(entry, dict) or entry.keys() - {"name", "quota", "keep_percent", "spend_cap"}:
            raise ConfigError(f"{where} must be a mapping of name, quota, keep_percent and spend_cap")
        name = entry.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ConfigError(f"{where}.name must be a nonempty executor name")
        if name in names:
            raise ConfigError(f"executors lists {name} twice")
        names.append(name)
        quota = entry.get("quota")
        if not isinstance(quota, str) or quota not in QUOTAS:
            raise ConfigError(f"{where}.quota must be subscription, api or unlimited")
        keep = entry.get("keep_percent")
        if keep is not None and (quota != "subscription" or type(keep) is not int or not 1 <= keep <= 100):
            raise ConfigError(f"{where}.keep_percent must be an integer from 1 to 100 on a subscription executor")
        cap = entry.get("spend_cap")
        if cap is not None and (quota == "unlimited" or isinstance(cap, bool)
                                or not isinstance(cap, (int, float)) or not cap > 0):
            raise ConfigError(f"{where}.spend_cap must be a number above 0 on a subscription or api executor")
        if quota == "api" and cap is None:
            raise ConfigError(f"{where}.spend_cap is required for an api executor; spending needs a daily cap")
    return set(names)


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_roles(roles: Any) -> None:
    """The roles source (#769): name -> agent, skills, memory, signals and optional settings."""
    from datetime import datetime

    from foragent.config import ConfigError

    if not isinstance(roles, dict):
        raise ConfigError("roles must be a mapping")
    for name, role in roles.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]+", name):
            raise ConfigError(f"invalid roles name: {name}")
        if not isinstance(role, dict) or role.keys() - {
            "agent", "skills", "memory", "signals", "scope", "created_at", "idle_days",
        }:
            raise ConfigError(f"invalid roles entry: {name}")
        for key in ("agent", "memory"):
            if role.get(key) is not None and not isinstance(role[key], str):
                raise ConfigError(f"roles.{name}.{key} must be a string")
        if not _strings(role.get("skills", [])):
            raise ConfigError(f"roles.{name}.skills must be a list of strings")
        scope = role.get("scope", [])
        if not isinstance(scope, str) and not _strings(scope):
            raise ConfigError(f"roles.{name}.scope must be a list of path globs")
        signals = role.get("signals", [])
        if not isinstance(signals, list) or any(
            not isinstance(signal, dict) or signal.keys() != {"paths", "kinds"}
            or not all(_strings(signal[key]) for key in ("paths", "kinds"))
            for signal in signals
        ):
            raise ConfigError(f"roles.{name}.signals must be a list of items with both paths and kinds string lists")
        if role.get("created_at") is not None and not isinstance(role["created_at"], (str, datetime)):
            raise ConfigError(f"roles.{name}.created_at must be a timestamp")
        idle = role.get("idle_days", 14)
        if type(idle) is not int or idle < 1:
            raise ConfigError(f"roles.{name}.idle_days must be a positive integer")
