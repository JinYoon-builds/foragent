"""Load repository settings over optional global settings."""

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import Any

import yaml


class ConfigError(ValueError):
    """Configuration could not be loaded or validated."""


SKILL_ROLES = {"style", "review", "tool", "domain"}


@dataclass(frozen=True)
class Config:
    review: str = "batch"
    review_paths: dict[str, str] = field(default_factory=dict)
    always_human: list[str] = field(default_factory=list)
    executor: str = "codex"
    executor_model: str | None = None
    executor_fallback_model: str | None = None
    lanes: dict[str, dict[str, Any]] = field(default_factory=dict)
    tiers: dict[str, dict[str, Any]] = field(default_factory=lambda: {
        "s": {"codex_effort": "medium", "claude_model": None},
        "m": {"codex_effort": "high", "claude_model": None},
        "l": {"codex_effort": "xhigh", "claude_model": None},
    })
    skill_libraries: list[Any] = field(default_factory=lambda: [{
        "name": "knowledge-work-plugins",
        "repo": "https://github.com/anthropics/knowledge-work-plugins",
    }, {
        "name": "skills",
        "repo": "https://github.com/anthropics/skills",
    }])
    skill_roles: dict[str, str] = field(default_factory=dict)
    max_parallel: int | None = None  # Optional ceiling; AIMD sets the value (D60).
    parallel_floor: int = 1
    test_slots: int = 2  # Machine-wide test command ceiling; AIMD lowers it under load (#243).
    test_slots_floor: int = 1
    transient_max_wait_minutes: int = 30
    limit_wait_minutes: int = 60
    codex_goal: bool = False
    reviewer: str | None = None
    reviewer_model: str | None = None
    review_max: int = 2
    review_level: str = "low"
    review_scope: str = "claude-local"
    test_command: str | None = None
    # Runner and premerge: auto trusts pull_request CI; ci always trusts it; all runs units locally.
    runner_unit_tests: str = "auto"
    approvers: list[str] = field(default_factory=list)
    acceptance_dir: str = "tests/acceptance/"
    notify: Any = None
    notify_summary_hour: int = 8
    run_hours: str = "always"
    stall_minutes: int = 30
    orchestrator_timeout_minutes: int = 30
    tick_failure_alert: int = 3
    max_runtime_minutes: int = 90
    max_retries: int = 2
    conflict_retries: int = 2
    base_branch: str = "main"
    repo: str | None = None
    board: str | None = None
    board_map: dict[str, str] | None = None
    language: str = "en"
    interview: str = "native"
    ouroboros_version: str | None = None
    profile: dict[str, Any] | None = None


DEFAULTS: dict[str, Any] = asdict(Config())
_REVIEW_LEVELS = {"fast", "batch", "careful"}
_EXECUTORS = {"codex", "claude"}


class LaneSettings:
    """Edit lanes in a settings file by lines; every untouched line keeps its bytes."""

    def __init__(self, path: Path):
        self.path = path
        try:
            # Bytes, not text mode: text mode would turn CRLF line ends into LF.
            text = path.read_bytes().decode("utf-8") if path.exists() else ""
            root = yaml.compose(text)
            data = yaml.safe_load(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        if root is not None and not isinstance(root, yaml.MappingNode):
            raise ConfigError(f"{path} must contain a mapping")
        self.root = root
        self.data: dict[str, Any] = data or {}
        # Split only where YAML breaks lines, so node marks index these lines.
        self.lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z", text)
        self.newline = next((line[len(line.rstrip("\r\n")):] for line in self.lines
                             if line.endswith(("\r", "\n"))), "\n")
        self.expected = deepcopy(self.data)
        self.comments = _comments(self.lines)

    def add_lane(self, name: str, lane: dict[str, Any], inherited: dict[str, dict[str, Any]]) -> None:
        """Add one lane after the file's last lane, or create `lanes` with inherited lanes."""
        found = _pair(self.root, "lanes")
        if found is not None and isinstance(found[1], yaml.MappingNode):
            key, lanes = found
            self.expected["lanes"] = {**self.data["lanes"], name: lane}
            if lanes.flow_style:
                self._flow_add(lanes, name, lane)
                return
            first_key, first_lane = lanes.value[0]
            indent = first_key.start_mark.column
            step = (first_lane.value[0][0].start_mark.column - indent
                    if _block(first_lane, yaml.MappingNode) else 2)
            self._insert(_last_line(*lanes.value[-1]) + 1, self._mapping({name: lane}, indent, step),
                         before=first_key.start_mark.line)
            return
        value = {**inherited, name: lane}
        self.expected["lanes"] = value
        if found is not None:
            # `lanes:` with no value: keep its line and put the lanes below it.
            key = found[0]
            self._insert(key.end_mark.line + 1, self._mapping(value, key.start_mark.column + 2, 2))
        elif self.root is not None and self.root.flow_style:
            self._flow_add(self.root, "lanes", value)
        else:
            block = ["lanes:" + self.newline, *self._mapping(value, 2, 2)]
            if self.root is not None:
                # After the last setting, so trailing comments stay last and keep their bytes.
                self._insert(_last_line(*self.root.value[-1]) + 1, block,
                             before=self.root.value[0][0].start_mark.line)
            elif self.lines and not self.lines[-1].endswith(("\r", "\n")):
                # Appending would add a line end to the final comment; put lanes first instead.
                self.lines[0:0] = block
            else:
                self._insert(len(self.lines), block)

    def set_skills(self, name: str, skills: list[str], lane: dict[str, Any],
                   inherited: dict[str, dict[str, Any]]) -> None:
        """Replace one lane's `skills` value in place, keeping comments around it."""
        found = _pair(self.root, "lanes")
        pair = _pair(found[1], name) if found and isinstance(found[1], yaml.MappingNode) else None
        if pair is None:
            self.add_lane(name, lane, inherited)
            return
        node = pair[1]
        if not isinstance(node, yaml.MappingNode):
            raise ConfigError(f"invalid lanes entry: {name}")
        self.expected["lanes"][name] = {**self.data["lanes"][name], "skills": skills}
        skill = _pair(node, "skills")
        if skill is not None and not _plain_skills(skill[1]):
            raise ConfigError(f"lane {name}: its skills in {self.path} use a layout fa cannot change without "
                              "touching other lines; edit .foragent.yml by hand")
        if skill is None and node.flow_style:
            self._flow_add(node, "skills", skills)
        elif skill is None:
            indent = node.value[0][0].start_mark.column
            self._insert(_last_line(*node.value[-1]) + 1, [" " * indent + "skills: " + _flow(skills) + self.newline],
                         before=node.value[0][0].start_mark.line)
        elif _block(skill[1], yaml.SequenceNode):
            self._replace_items(skill[0], skill[1], skills)
        elif _empty(skill[1]):
            self._after_colon(skill[0], _flow(skills))
        else:
            self._replace(skill[1], _flow(skills))

    def check(self) -> str:
        """Return the edited text after confirming it reads back as exactly the intended settings."""
        text = "".join(self.lines)
        try:
            written = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            written = None
        if written != self.expected:
            raise ConfigError(f"cannot update lanes in {self.path} without rewriting other lines; edit it by hand")
        if self.comments - _comments(self.lines):
            raise ConfigError(f"cannot update lanes in {self.path} without changing or losing a comment; edit it by hand")
        return text

    def save(self) -> None:
        self.path.write_bytes(self.check().encode("utf-8"))

    def _mapping(self, lanes: dict[str, Any], indent: int, step: int) -> list[str]:
        lines = []
        for name, lane in lanes.items():
            if not isinstance(lane, dict):
                lines.append(f"{' ' * indent}{_flow(name)}: {_flow(lane)}{self.newline}")
                continue
            lines.append(f"{' ' * indent}{_flow(name)}:{self.newline}")
            lines += [f"{' ' * (indent + step)}{_flow(key)}: {_flow(value)}{self.newline}"
                      for key, value in lane.items()]
        return lines

    def _insert(self, line: int, lines: list[str], *, before: int | None = None) -> None:
        """Insert whole lines at `line`, or at `before` when that would add a line end to the file's last line."""
        if not lines:
            return
        if 0 < line <= len(self.lines) and not self.lines[line - 1].endswith(("\r", "\n")):
            if before is None:
                raise ConfigError(f"cannot update lanes in {self.path} without changing its last line, "
                                  "which has no line end; edit it by hand")
            line = before
        self.lines[line:line] = lines

    def _flow_add(self, mapping: yaml.MappingNode, key: str, value: Any) -> None:
        """Add `key: value` to a `{...}` mapping right after its last value, leaving the rest as is."""
        entry = f"{_flow(key)}: {_flow(value)}"
        if mapping.value:
            mark, text = mapping.value[-1][1].end_mark, ", " + entry
        else:
            mark, text = mapping.start_mark, entry
            mark = yaml.Mark(mark.name, mark.index + 1, mark.line, mark.column + 1, None, None)  # after `{`
        line = self.lines[mark.line]
        self.lines[mark.line] = line[:mark.column] + text + line[mark.column:]

    def _replace(self, node: yaml.Node, text: str) -> None:
        start, end = node.start_mark, node.end_mark
        self.lines[start.line:end.line + 1] = [
            self.lines[start.line][:start.column] + text + self.lines[end.line][end.column:]]

    def _after_colon(self, key: yaml.Node, text: str) -> None:
        line = self.lines[key.end_mark.line]
        colon = line.index(":", key.end_mark.column) + 1
        self.lines[key.end_mark.line] = line[:colon] + " " + text + line[colon:]

    def _replace_items(self, key: yaml.Node, sequence: yaml.SequenceNode, skills: list[str]) -> None:
        """Keep unchanged item lines; removed items leave their comments behind."""
        items = sequence.value
        old = [item.value if isinstance(item, yaml.ScalarNode) else None for item in items]
        kept = [value for value in old if value in skills]
        keep = (len(set(kept)) == len(kept) and skills[:len(kept)] == kept
                and all(item.start_mark.line == item.end_mark.line for item in items))
        added = skills[len(kept):] if keep else skills
        first = self.lines[items[0].start_mark.line]
        dash = " " * first.rindex("-", 0, items[0].start_mark.column)
        self._insert(_last_line(items[-1], items[-1]) + 1,
                     [f"{dash}- {_flow(value)}{self.newline}" for value in added])
        for item in reversed(items):
            if keep and item.value in skills:
                continue
            start, end = item.start_mark, item.end_mark
            line = self.lines[end.line]
            if start.line == end.line and line[end.column:].lstrip(" \t").startswith("#"):
                # Blank out `- item` only; the comment keeps its column and bytes.
                column = line.rindex("-", 0, start.column)
                self.lines[end.line] = line[:column] + " " * (end.column - column) + line[end.column:]
            else:
                del self.lines[start.line:end.line + 1]
        if not skills:
            self._after_colon(key, "[]")


def _plain_skills(node: yaml.Node) -> bool:
    """Whether a skills value has a shape the line edits handle: one-line scalars or a one-line `[...]`."""
    def one_line(item: yaml.Node) -> bool:
        return (isinstance(item, yaml.ScalarNode) and item.style in (None, "'", '"')
                and item.start_mark.line == item.end_mark.line)

    if isinstance(node, yaml.SequenceNode) and not node.flow_style:
        return all(one_line(item) for item in node.value)
    if isinstance(node, yaml.SequenceNode):
        return node.start_mark.line == node.end_mark.line
    return _empty(node) or one_line(node)


def _comments(lines: list[str]) -> Counter:
    """Count each comment's exact bytes (from `#` through its line end) in settings lines."""
    found: Counter = Counter()
    for line in lines:
        quote, previous = None, " "
        for index, char in enumerate(line):
            if quote:
                quote = None if char == quote else quote
            elif char in "'\"" and previous in " \t[{,:":
                quote = char
            elif char == "#" and previous in " \t":
                found[line[index:]] += 1
                break
            previous = char
    return found


def _pair(mapping: yaml.Node | None, name: str) -> tuple[yaml.Node, yaml.Node] | None:
    if not isinstance(mapping, yaml.MappingNode):
        return None
    return next(((key, value) for key, value in mapping.value
                 if isinstance(key, yaml.ScalarNode) and key.value == name), None)


def _block(node: yaml.Node | None, kind: type) -> bool:
    return isinstance(node, kind) and not node.flow_style and bool(node.value)


def _empty(node: yaml.Node) -> bool:
    return isinstance(node, yaml.ScalarNode) and node.start_mark.index == node.end_mark.index


def _last_line(key: yaml.Node, value: yaml.Node) -> int:
    """Return the last source line of a key's value, before any following comments."""
    if _block(value, yaml.MappingNode):
        return _last_line(*value.value[-1])
    if _block(value, yaml.SequenceNode):
        return _last_line(value.value[-1], value.value[-1])
    if _empty(value):
        return key.end_mark.line
    end = value.end_mark
    return end.line - 1 if end.column == 0 and end.line > value.start_mark.line else end.line


def _flow(value: Any) -> str:
    """Render a value on one line, as `fa` writes lane settings."""
    text = yaml.safe_dump(value, default_flow_style=True, sort_keys=False, allow_unicode=True, width=1 << 30)
    if text.endswith("\n...\n"):
        text = text[:-5]
    return text.strip()


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
    if "max_parallel" in value and value["max_parallel"] is None:
        raise ConfigError("max_parallel must be a positive integer; omit it to have no ceiling")
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
    for key in ("executor", "reviewer"):
        value = values[key]
        if key == "reviewer" and value is None:
            continue
        allowed = _EXECUTORS | {"none"} if key == "reviewer" else _EXECUTORS
        if not isinstance(value, str) or value not in allowed:
            raise ConfigError(f"{key} must be codex or claude" + (" or none" if key == "reviewer" else ""))
    for key in ("stall_minutes", "max_runtime_minutes", "max_retries", "conflict_retries", "review_max",
                "max_parallel", "transient_max_wait_minutes", "limit_wait_minutes",
                "orchestrator_timeout_minutes", "tick_failure_alert", "test_slots", "test_slots_floor",
                "parallel_floor"):
        if key == "max_parallel" and values[key] is None:
            continue
        if type(values[key]) is not int or values[key] <= 0:
            raise ConfigError(f"{key} must be a positive integer")
    if values["test_slots_floor"] > values["test_slots"]:
        raise ConfigError("test_slots_floor must be <= test_slots")
    if values["max_parallel"] is not None and values["parallel_floor"] > values["max_parallel"]:
        raise ConfigError("parallel_floor must be <= max_parallel")
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
        if not isinstance(lane, dict) or lane.keys() - {"paths", "agent", "skills", "executor", "model", "title", "emoji"}:
            raise ConfigError(f"invalid lanes entry: {name}")
        paths = lane.get("paths", [])
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ConfigError(f"lanes.{name}.paths must be a list of strings")
        for key in ("agent", "executor", "model", "title", "emoji"):
            if lane.get(key) is not None and not isinstance(lane[key], str):
                raise ConfigError(f"lanes.{name}.{key} must be a string or null")
        if lane.get("executor") is not None and lane["executor"] not in _EXECUTORS:
            raise ConfigError(f"lanes.{name}.executor must be codex, claude, or null")
        if not isinstance(lane.get("skills", []), list) or any(
            not isinstance(skill, str) for skill in lane.get("skills", [])
        ):
            raise ConfigError(f"lanes.{name}.skills must be a list of strings")
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


def load_config(repo_root: Path, global_path: Path | None = None) -> Config:
    local_path = repo_root / ".foragent.yml"
    if not local_path.is_file():
        raise ConfigError("not a Foragent repo: .foragent.yml missing")
    if global_path is None:
        global_path = Path.home() / ".config/foragent/config.yml"
    values = deepcopy(DEFAULTS)
    if global_path.exists():
        _overlay(values, _read_yaml(global_path))
    _overlay(values, _read_yaml(local_path))
    _validate(values)
    return Config(**values)


def _overlay(values: dict[str, Any], overrides: dict[str, Any]) -> None:
    tiers = overrides.get("tiers")
    if isinstance(tiers, dict):
        previous = values["tiers"]
        if not isinstance(previous, dict):
            raise ConfigError("tiers must define s, m, and l")
        overrides = {**overrides, "tiers": {
            **previous, **{name: {**(previous[name] if isinstance(previous.get(name), dict) else {}), **tier}
                           if isinstance(tier, dict) else tier for name, tier in tiers.items()},
        }}
    values.update(overrides)


def load_global_config(global_path: Path | None = None) -> Config:
    path = global_path or Path.home() / ".config/foragent/config.yml"
    values = deepcopy(DEFAULTS)
    if path.exists():
        _overlay(values, _read_yaml(path))
    _validate(values)
    return Config(**values)


def register_skill_library(name: str, repo: str, roles: dict[str, str]) -> Config:
    """Persist an approved library and roles, preserving unrelated global settings."""
    path = Path.home() / ".config/foragent/config.yml"
    config = load_global_config(path)
    values = _read_yaml(path) if path.exists() else {}
    libraries = deepcopy(config.skill_libraries)
    if not any((entry if isinstance(entry, str) else entry["repo"]) == repo for entry in libraries):
        libraries.append({"name": name, "repo": repo})
    values.update(skill_libraries=libraries, skill_roles={**config.skill_roles, **roles})
    registered = {**asdict(config), "skill_libraries": libraries, "skill_roles": values["skill_roles"]}
    _validate(registered)
    content = yaml.safe_dump(values, sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return Config(**registered)


def set_profile(repo_root: Path, *, kind: str, stage: str, priority: str) -> dict[str, str]:
    """Save the confirmed project profile without copying global settings locally."""
    load_config(repo_root)
    path = repo_root / ".foragent.yml"
    local = _read_yaml(path)
    profile = {"kind": kind, "stage": stage, "priority": priority,
               "confirmed_at": date.today().isoformat()}
    local["profile"] = profile
    path.write_text(yaml.safe_dump(local, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return profile


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
