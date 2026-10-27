"""Standing roles: one ``roles:`` source in .foragent.yml and the files derived from it (D95).

A role is an agent file, its skills, project memory and the signals that call it.
Only this module writes ``roles``; the agent file and memory directory are written
in the same command, and ``fa doctor`` reports a projection that drifted.
"""

from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
import re
import shutil
import sys
from typing import Any

import yaml

from foragent.config import Config, ConfigError, _comments, _last_line, _pair, load_config
from foragent.config_validation import _validate
from foragent.git import GitError, GitRepo
from foragent.paths import repo_path


DEFAULT_ROLES = ("orchestrator", "architect", "docs")
# Names of state files a role session record (<name>.json) would collide with.
STATE_NAMES = frozenset({"plan", "session", "usage", "tidy", "issues", "capacity"})
IDLE_DAYS = 14
ARCHITECT_KINDS = ["repeated_design_holds", "hot_overlap_files", "files_over_limit", "idle_lanes_with_backlog"]
DOCS_PATHS = ["docs/**", "README*"]
SIGNAL_KEYS = ("paths", "kinds")


class RoleError(ConfigError):
    """A role cannot be added, changed or removed as requested."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def memory_dir(name: str) -> str:
    return f".claude/agent-memory/fa-{name}/"


def default_entry(name: str, skills: list[str] | None = None) -> dict[str, Any]:
    signals = {"architect": [{"paths": [], "kinds": list(ARCHITECT_KINDS)}],
               "docs": [{"paths": list(DOCS_PATHS), "kinds": []}]}.get(name, [])
    entry = {"agent": f"fa-{name}", "skills": [f"fa-{name}"] if skills is None else list(skills),
             "memory": memory_dir(name), "signals": signals}
    if name == "docs":
        entry["scope"] = list(DOCS_PATHS)
    entry["created_at"] = _now()
    return entry


def load_roles(config: Config) -> dict[str, dict[str, Any]]:
    """Every configured role with its optional fields filled in."""
    roles = {}
    for name, entry in config.roles.items():
        scope = entry.get("scope", [])
        roles[name] = {
            "agent": entry.get("agent") or f"fa-{name}",
            "skills": list(entry.get("skills", [])),
            "memory": entry.get("memory") or memory_dir(name),
            "signals": deepcopy(entry.get("signals", [])),
            "scope": [scope] if isinstance(scope, str) else list(scope),
            "created_at": entry.get("created_at"),
            "idle_days": entry.get("idle_days", IDLE_DAYS),
        }
    return roles


def _split_agent(path: Path) -> tuple[dict | None, str]:
    """Agent frontmatter (None when absent or invalid) and the body after it."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, ""
    match = re.match(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|$)", content, re.S | re.M)
    try:
        header = yaml.safe_load(match[1]) if match else None
    except yaml.YAMLError:
        header = None
    return (header if isinstance(header, dict) else None), (content[match.end():] if match else content)


def with_defaults(root: Path, roles: dict[str, Any]) -> dict[str, Any] | None:
    """``roles`` plus any missing default role, or None when nothing is missing.

    A missing entry takes its skills from an existing agent file, which the person owns.
    """
    missing = [name for name in DEFAULT_ROLES if name not in roles]
    if not missing:
        return None
    result = deepcopy(roles)
    for name in missing:
        header, _ = _split_agent(repo_path(root, f".claude/agents/fa-{name}.md"))
        skills = header.get("skills") if header else None
        valid = isinstance(skills, list) and all(isinstance(skill, str) for skill in skills)
        result[name] = default_entry(name, skills if valid else None)
    return result


def replace_block(text: str, roles: dict[str, Any]) -> str:
    """Rewrite only the top-level ``roles`` block of a settings text, or append one."""
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z", text)
    try:
        node = yaml.compose(text)
        before = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RoleError(f"cannot read .foragent.yml: {exc}") from exc
    block = yaml.safe_dump({"roles": roles}, sort_keys=False, allow_unicode=True)
    pair = _pair(node, "roles")
    if pair is None:
        prefix = "".join(lines)
        edited = prefix + ("\n" if prefix and not prefix.endswith("\n") else "") + block
        kept = lines
    else:
        start, end = pair[0].start_mark.line, _last_line(*pair) + 1
        if end < len(lines) or lines[-1].endswith(("\r", "\n")):
            edited = "".join(lines[:start]) + block + "".join(lines[end:])
        else:
            edited = "".join(lines[:start]) + block
        kept = lines[:start] + lines[end:]
    try:
        written = yaml.safe_load(edited)
    except yaml.YAMLError:
        written = None
    lost = _comments(kept) - _comments(re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z", edited))
    if not isinstance(before, dict) or written != {**before, "roles": roles} or lost:
        raise RoleError("cannot update roles in .foragent.yml without changing other lines; edit it by hand")
    return edited


def role_drift(root: Path, config: Config) -> list[str]:
    """Roles whose agent frontmatter skills differ from the roles source. Only reports."""
    problems = []
    for name, entry in load_roles(config).items():
        relative = f".claude/agents/{entry['agent']}.md"
        path = repo_path(root, relative)
        if not path.is_file():
            problems.append(f"FAIL role {name}: agent file {relative} is missing; "
                            f"run `fa role set {name} --skill ...` to rewrite it")
            continue
        header, _ = _split_agent(path)
        declared = header.get("skills") if header else None
        if declared != entry["skills"]:
            problems.append(f"FAIL role {name}: skills differ: .foragent.yml={entry['skills']!r}, "
                            f"{relative}={declared!r}; run `fa role set {name} --skill ...` to rewrite both")
    return problems


def parse_signals(items: list[str]) -> list[dict[str, list[str]]]:
    signals = []
    for item in items:
        key, separator, value = item.partition(":")
        if not separator or key not in SIGNAL_KEYS or not value.strip():
            raise RoleError(f"invalid --signal {item!r}: use paths:<glob> or kinds:<fact kind>")
        signals.append({"paths": [value.strip()] if key == "paths" else [],
                        "kinds": [value.strip()] if key == "kinds" else []})
    return signals


def _description(name: str) -> str:
    return f"Foragent {name} role"


def _agent_text(name: str, skills: list[str]) -> str:
    header = yaml.safe_dump({"name": f"fa-{name}", "description": _description(name), "memory": "project",
                             "skills": skills}, sort_keys=False, allow_unicode=True)
    return (f"---\n{header}---\n"
            f"You are this repository's Foragent {name}; follow your role skills.\n"
            "Record durable, project-specific lessons (patterns, pitfalls, commands) in your memory.\n"
            "Propose changes to the orchestrator; never message a person directly.\n")


class _Edit:
    """One role command: load, check the branch, write the source and its files, commit."""

    def __init__(self, root: Path, git: GitRepo | None, manager=None):
        self.root = Path(root).resolve()
        self.config = load_config(self.root)
        self.git = git or GitRepo(self.root)
        self.manager = manager
        self.path = repo_path(self.root, ".foragent.yml")
        self.text = self.path.read_bytes().decode("utf-8")
        self.roles = deepcopy((yaml.safe_load(self.text) or {}).get("roles") or {})

    def check_branch(self, verb: str) -> None:
        if self.git.current_branch() != self.config.base_branch:
            raise GitError(f"fa role {verb} must run on the base branch {self.config.base_branch!r}")

    def check_skills(self, skills: list[str]) -> list:
        if not skills:
            return []
        if self.manager is None:
            from foragent.skills import SkillManager
            self.manager = SkillManager(self.config)
        selected = [self.manager.check_install(identity, self.root) for identity in skills]
        names = [skill.path.name for skill in selected]
        if len(set(names)) != len(names):
            raise RoleError("duplicate role skills or conflicting installation names")
        return names

    def save(self, roles: dict[str, Any], skills: list[str]) -> None:
        edited = replace_block(self.text, roles)
        _validate({**asdict(self.config), "roles": roles})
        for identity in skills:
            self.manager.install(identity, self.root)
        self.path.write_bytes(edited.encode("utf-8"))

    def event(self, kind: str, name: str, why: str | None) -> None:
        from foragent import orch_events
        from foragent.state import StateStore
        try:
            repo = self.config.repo or self.git.remote_slug()
        except GitError as exc:
            # The role is already written and committed; only its journal line has no home.
            print(f"Warning: no orch event for role {name}: {exc}", file=sys.stderr)
            return
        store = StateStore.for_repo(repo)
        orch_events.append_event(store, datetime.now(UTC), None, kind, f"{name}: {why or ''}", role=name, why=why)


def add_role(root: Path, name: str, *, skills: list[str], scope: list[str], signals: list[str],
             why: str | None = None, idle_days: int | None = None, git: GitRepo | None = None,
             manager=None) -> dict[str, Any]:
    edit = _Edit(root, git, manager)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise RoleError(f"invalid role name: {name}")
    agent = f".claude/agents/fa-{name}.md"
    if name in edit.roles:
        raise RoleError(f"role already exists: {name}")
    if name in edit.config.lanes:
        raise RoleError(f"role name clashes with lane {name}")
    if name in STATE_NAMES:
        raise RoleError(f"role name clashes with the state file {name}.json")
    if repo_path(edit.root, agent).exists():
        raise RoleError(f"agent already exists: {agent}")
    edit.check_branch("add")
    installed = edit.check_skills(skills)
    entry = {"agent": f"fa-{name}", "skills": installed, "memory": memory_dir(name),
             "signals": parse_signals(signals), "scope": list(scope), "created_at": _now()}
    if idle_days is not None:
        entry["idle_days"] = idle_days
    edit.save({**edit.roles, name: entry}, skills)
    destination = repo_path(edit.root, agent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_agent_text(name, installed), encoding="utf-8")
    memory = repo_path(edit.root, memory_dir(name) + "MEMORY.md")
    memory.parent.mkdir(parents=True, exist_ok=True)
    if not memory.exists():
        memory.write_text(f"# fa-{name} memory\n", encoding="utf-8")
    published = [".foragent.yml", agent, memory_dir(name) + "MEMORY.md", *([".gitignore"] if installed else [])]
    edit.git.ensure_tracked(published)
    edit.git.commit_paths(published, f"Foragent role {name}")
    edit.event("role-added", name, why)
    return entry


def set_role(root: Path, name: str, *, skills: list[str] | None = None, scope: list[str] | None = None,
             signals: list[str] | None = None, why: str | None = None, idle_days: int | None = None,
             git: GitRepo | None = None, manager=None) -> dict[str, Any]:
    """Replace the given settings; regenerate only the agent frontmatter, keeping its body."""
    edit = _Edit(root, git, manager)
    if name not in edit.roles:
        raise RoleError(f"unknown role: {name}")
    entry = deepcopy(edit.roles[name])
    agent = f".claude/agents/{entry.get('agent') or f'fa-{name}'}.md"
    edit.check_branch("set")
    installed = edit.check_skills(skills) if skills is not None else None
    if installed is not None:
        entry["skills"] = installed
    if scope is not None:
        entry["scope"] = list(scope)
    if signals is not None:
        entry["signals"] = parse_signals(signals)
    if idle_days is not None:
        entry["idle_days"] = idle_days
    header, body = _split_agent(repo_path(edit.root, agent))
    header = {"name": entry.get("agent") or f"fa-{name}", "memory": "project", **(header or {}),
              "description": _description(name), "skills": entry.get("skills", [])}
    if not body:
        body = _agent_text(name, header["skills"]).split("---\n", 2)[2]
    edit.save({**edit.roles, name: entry}, skills or [])
    destination = repo_path(edit.root, agent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("---\n" + yaml.safe_dump(header, sort_keys=False, allow_unicode=True) + "---\n" + body,
                           encoding="utf-8")
    published = [".foragent.yml", agent, *([".gitignore"] if installed else [])]
    edit.git.ensure_tracked(published)
    edit.git.commit_paths(published, f"Foragent role {name}")
    edit.event("role-set", name, why)
    return entry


def remove_role(root: Path, name: str, *, git: GitRepo | None = None) -> None:
    """Delete the entry, the agent file and the whole memory directory."""
    edit = _Edit(root, git)
    if name in DEFAULT_ROLES:
        raise RoleError(f"the default {name} role cannot be removed")
    if name not in edit.roles:
        raise RoleError(f"unknown role: {name}")
    entry = edit.roles[name]
    agent = f".claude/agents/{entry.get('agent') or f'fa-{name}'}.md"
    memory = (entry.get("memory") or memory_dir(name)).rstrip("/")
    memory_path = repo_path(edit.root, memory)
    if not memory.startswith(".claude/agent-memory/") or memory_path == repo_path(edit.root, ".claude/agent-memory"):
        raise RoleError(f"role {name}: memory {memory!r} is outside .claude/agent-memory/")
    edit.check_branch("remove")
    tracked = [path for path in edit.git._call(["ls-files", "--cached", "-z", "--", agent, memory]).split("\0") if path]
    roles = {key: value for key, value in edit.roles.items() if key != name}
    edit.save(roles, [])
    repo_path(edit.root, agent).unlink(missing_ok=True)
    if memory_path.is_dir():
        shutil.rmtree(memory_path)
    edit.git.commit_paths([".foragent.yml", *tracked], f"Foragent role {name} removed")
