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

from foragent.config_validation import _validate


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
    # Cautious start: one conflict (#196) in 231 runs; the orchestrator
    # changes this only from conflict facts (fa orch facts).
    overlap_per_file: int = 2
    test_slots: int = 2  # Machine-wide test command ceiling; AIMD lowers it under load (#243).
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
    checks: str = "local"
    checks_fallback: str | None = None
    ci_shards: int = 4
    ci_runs_on: str | list[str] = "ubuntu-latest"
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
    update_check: bool = True  # The daily release check (#417); FORAGENT_UPDATE_CHECK=off also opts out.
    profile: dict[str, Any] | None = None


DEFAULTS: dict[str, Any] = asdict(Config())
# Retired floors (#497): still accepted in YAML, dropped before Config is built.
IGNORED_KEYS = frozenset({"test_slots_floor", "parallel_floor"})
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
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return _parse_yaml(content, path)


def _parse_yaml(content: str, path: Path | str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"configuration must be a mapping: {path}")
    for key in value:
        if key not in DEFAULTS and key not in IGNORED_KEYS:
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


# Built from the local checkout once per command; origin's copy cannot move them.
LOCAL_ONLY_KEYS = ("repo", "base_branch", "language")


def load_committed_config(local: Config, content: str | None, source: str,
                          global_path: Path | None = None) -> tuple[Config | None, list[str]]:
    """Merge a committed .foragent.yml (``content``) with the global config like load_config.

    ``local`` is the config already loaded from the checkout. Returns ``(None, [])``
    when ``content`` is missing, unparsable or invalid, so the caller keeps ``local``.
    Otherwise the second item names the local-only keys origin tried to change;
    those keys keep their local values and the rest of origin's settings apply.
    """
    if content is None:
        return None, []
    if global_path is None:
        global_path = Path.home() / ".config/foragent/config.yml"
    try:
        committed = _parse_yaml(content, source)
        values = deepcopy(DEFAULTS)
        if global_path.exists():
            _overlay(values, _read_yaml(global_path))
        _overlay(values, committed)
        # A language change is not a conflict worth a warning; repo and base_branch are.
        ignored = [key for key in ("repo", "base_branch") if key in committed and values[key] != getattr(local, key)]
        values.update({key: getattr(local, key) for key in LOCAL_ONLY_KEYS})
        _validate(values)
        return Config(**values), ignored
    except (ConfigError, TypeError, ValueError):
        return None, []


def _overlay(values: dict[str, Any], overrides: dict[str, Any]) -> None:
    overrides = {key: value for key, value in overrides.items() if key not in IGNORED_KEYS}
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
