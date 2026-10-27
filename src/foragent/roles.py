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
import subprocess
import sys
from typing import Any

import yaml

from foragent import role_agents
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
    signals = {"architect": [{"paths": [], "kinds": [kind]} for kind in ARCHITECT_KINDS],
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


_LINES = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")


def _dump(value: dict[str, Any], indent: int, newline: str = "\n") -> list[str]:
    text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
    return [" " * indent + line + newline for line in text.splitlines()]


def edit_roles(text: str, roles: dict[str, Any]) -> str:
    """Rewrite only the top-level ``roles`` block, as block-style YAML at its current position.

    With no ``roles`` key the block is appended at the end of the file. Every byte outside
    the block stays identical; comments inside it are dropped, and one line says so.
    """
    lines = _LINES.findall(text)
    try:
        node = yaml.compose(text)
        before = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RoleError(f"cannot read .foragent.yml: {exc}") from exc
    if not isinstance(before, dict):
        raise RoleError(".foragent.yml must contain a mapping")
    # The file's own line end, so a CRLF file stays CRLF.
    newline = next((line[len(line.rstrip("\r\n")):] for line in lines if line.endswith(("\r", "\n"))), "\n")
    pair = _pair(node, "roles")
    ends = [event.start_mark.line for event in yaml.parse(text)
            if isinstance(event, yaml.events.DocumentEndEvent) and event.explicit]
    if pair is None and ends:
        # An explicit document end (`...`): the block goes right before it, inside the document.
        start = end = ends[0]
        column = 0
    elif pair is None:
        start = end = len(lines)
        if lines and not lines[-1].endswith(("\r", "\n")):
            lines[-1] += newline
        column = 0
    else:
        start, end, column = pair[0].start_mark.line, _last_line(*pair) + 1, pair[0].start_mark.column
    dropped = sum(_comments(lines[start:end]).values())
    lines[start:end] = _dump({"roles": roles}, column, newline)
    edited = "".join(lines)
    try:
        written = yaml.safe_load(edited)
    except yaml.YAMLError:
        written = None
    if written != {**before, "roles": roles}:
        raise RoleError("cannot rewrite the roles block of .foragent.yml; edit it by hand")
    if dropped:
        print(f"Note: rewrote the roles block of .foragent.yml and dropped {dropped} comment(s) inside it.",
              file=sys.stderr)
    return edited


def _memory_file(root: Path, name: str, memory: str) -> str:
    """The role's MEMORY.md path; the memory directory must sit inside .claude/agent-memory/."""
    directory = memory.rstrip("/")
    if not directory.startswith(".claude/agent-memory/") or (
            repo_path(root, directory) == repo_path(root, ".claude/agent-memory")):
        raise RoleError(f"role {name}: memory {memory!r} is outside .claude/agent-memory/")
    return directory + "/MEMORY.md"


def _ensure_memory(root: Path, name: str, entry: dict[str, Any]) -> str:
    """Create the role's MEMORY.md when missing (an existing one, even empty, is kept); return its path."""
    relative = _memory_file(root, name, entry.get("memory") or memory_dir(name))
    memory = repo_path(root, relative)
    if not memory.exists():
        memory.parent.mkdir(parents=True, exist_ok=True)
        agent = entry.get("agent") or f"fa-{name}"
        memory.write_text(role_agents.MEMORY.get(agent, f"# {agent} memory\n"), encoding="utf-8")
    return relative


def role_drift(root: Path, config: Config) -> list[str]:
    """Roles whose agent file or memory is missing, or whose agent skills differ from the roles source.

    Only reports; ``fa role set`` rewrites the missing or drifted files.
    """
    problems = []
    for name, entry in load_roles(config).items():
        memory = entry["memory"].rstrip("/") + "/MEMORY.md"
        if not repo_path(root, memory).is_file():
            problems.append(f"FAIL role {name}: memory {memory} is missing; run `fa role set {name}` to create it")
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
    """One signal item per --signal; every item carries both paths and kinds (the unused one empty)."""
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


def _local_git(root: Path):
    """A runner for role commands' local-only Git calls (no fetch or push).

    Without a timeout, subprocess waits for git in one blocking wait; with one, it
    polls with sleeps and kills git if anything interrupts that wait.
    """
    def run(args: list[str]) -> str:
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                              errors="replace").stdout
    return run


class _Edit:
    """One role command: load, check the branch, write the source and its files, commit."""

    def __init__(self, root: Path, git: GitRepo | None, manager=None):
        self.root = Path(root).resolve()
        self.config = load_config(self.root)
        self.git = git or GitRepo(self.root, _local_git(self.root))
        self.manager = manager
        self.to_install: list[str] = []
        self.path = repo_path(self.root, ".foragent.yml")
        self.text = self.path.read_bytes().decode("utf-8")
        self.roles = deepcopy((yaml.safe_load(self.text) or {}).get("roles") or {})

    def check_branch(self, verb: str) -> None:
        if self.git.current_branch() != self.config.base_branch:
            raise GitError(f"fa role {verb} must run on the base branch {self.config.base_branch!r}")

    def check_skills(self, skills: list[str]) -> list[str]:
        """Installed skill names; each resolves as a repo product skill, a library skill or an installed copy.

        Skills that still need copying are kept in ``self.to_install``, like the lanes code copies them.
        """
        from foragent.skills import SkillError, SkillManager

        self.to_install = []
        names = []
        for identity in skills:
            if self.manager is None:
                self.manager = SkillManager(self.config)
            try:
                names.append(self.manager.check_install(identity, self.root).path.name)
                self.to_install.append(identity)
            except SkillError:
                installed = repo_path(self.root, f".claude/skills/{identity}")
                if installed.is_symlink() or not (installed / "SKILL.md").is_file():
                    raise
                names.append(identity)
        if len(set(names)) != len(names):
            raise RoleError("duplicate role skills or conflicting installation names")
        return names

    def save(self, roles: dict[str, Any]) -> None:
        edited = edit_roles(self.text, roles)
        _validate({**asdict(self.config), "roles": roles})
        for identity in self.to_install:
            self.manager.install(identity, self.root)
        self.path.write_bytes(edited.encode("utf-8"))

    def publish(self, owned: list[str], title: str) -> list[str]:
        """Commit the whole current state of the paths a role owns, against HEAD.

        ``owned`` is the role's settings file, agent file and memory directory (plus
        .gitignore for new skill copies). Edits and deletions made by hand before the
        command are staged too (``git add -A``), not only the files this command wrote.
        Nothing to commit is no error: the orch event never depends on a commit.
        """
        def listed(args: list[str]) -> list[str]:
            return [path for path in self.git._call([*args, "-z", "--", *owned]).split("\0") if path]

        def under(files: list[str], path: str) -> bool:
            return any(file == path or file.startswith(path.rstrip("/") + "/") for file in files)

        # git add only takes paths the worktree or index still has; a deletion already
        # staged (git rm) is known only to HEAD and is picked up by the diff below.
        index = listed(["ls-files", "--cached"])
        stageable = [path for path in owned if repo_path(self.root, path).exists() or under(index, path)]
        if stageable:
            self.git._call(["add", "-A", "-f", "--", *stageable])
        changed = listed(["diff", "--cached", "--name-only", "--no-renames", "HEAD"])
        if changed:
            self.git.commit_paths(changed, title)
        return changed

    def store(self):
        """The orchestrator journal's store; a repository without a GitHub origin uses its path as the key."""
        from foragent.state import StateStore
        try:
            repo = self.config.repo or self.git.remote_slug()
        except GitError:
            repo = "local" + self.root.as_posix()
        return StateStore.for_repo(repo)

    def event(self, store, kind: str, name: str, why: str | None) -> None:
        from foragent import orch_events
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
    store = edit.store()
    installed = edit.check_skills(skills)
    entry = {"agent": f"fa-{name}", "skills": installed, "memory": memory_dir(name),
             "signals": parse_signals(signals), "scope": list(scope), "created_at": _now()}
    if idle_days is not None:
        entry["idle_days"] = idle_days
    memory = _memory_file(edit.root, name, entry["memory"])
    owned = [".foragent.yml", agent, memory.removesuffix("/MEMORY.md"), *([".gitignore"] if edit.to_install else [])]
    edit.save({**edit.roles, name: entry})
    destination = repo_path(edit.root, agent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_agent_text(name, installed), encoding="utf-8")
    _ensure_memory(edit.root, name, entry)
    edit.publish(owned, f"Foragent role {name}")
    edit.event(store, "role-added", name, why)
    return entry


def set_role(root: Path, name: str, *, skills: list[str] | None = None, scope: list[str] | None = None,
             signals: list[str] | None = None, why: str | None = None, idle_days: int | None = None,
             git: GitRepo | None = None, manager=None) -> dict[str, Any]:
    """Replace the given settings; regenerate only the agent frontmatter, keeping its body.

    A missing agent file or memory is derived again, so set also repairs what doctor reports.
    """
    edit = _Edit(root, git, manager)
    if name not in edit.roles:
        raise RoleError(f"unknown role: {name}")
    entry = deepcopy(edit.roles[name])
    agent = f".claude/agents/{entry.get('agent') or f'fa-{name}'}.md"
    edit.check_branch("set")
    store = edit.store()
    installed = edit.check_skills(skills) if skills is not None else None
    if installed is not None:
        entry["skills"] = installed
    if scope is not None:
        entry["scope"] = list(scope)
    if signals is not None:
        entry["signals"] = parse_signals(signals)
    if idle_days is not None:
        entry["idle_days"] = idle_days
    agent_path = repo_path(edit.root, agent)
    header, body = _split_agent(agent_path)
    header = {"name": entry.get("agent") or f"fa-{name}", "memory": "project", **(header or {}),
              "description": _description(name), "skills": entry.get("skills", [])}
    if not agent_path.exists():
        # Only a missing agent gets the generated body; an existing one, even empty, keeps its own.
        body = _agent_text(name, header["skills"]).split("---\n", 2)[2]
    memory = _memory_file(edit.root, name, entry.get("memory") or memory_dir(name))
    owned = [".foragent.yml", agent, memory.removesuffix("/MEMORY.md"), *([".gitignore"] if edit.to_install else [])]
    edit.save({**edit.roles, name: entry})
    _ensure_memory(edit.root, name, entry)
    destination = repo_path(edit.root, agent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("---\n" + yaml.safe_dump(header, sort_keys=False, allow_unicode=True) + "---\n" + body,
                           encoding="utf-8")
    edit.publish(owned, f"Foragent role {name}")
    edit.event(store, "role-set", name, why)
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
    memory = _memory_file(edit.root, name, entry.get("memory") or memory_dir(name)).removesuffix("/MEMORY.md")
    memory_path = repo_path(edit.root, memory)
    edit.check_branch("remove")
    roles = {key: value for key, value in edit.roles.items() if key != name}
    edit.save(roles)
    repo_path(edit.root, agent).unlink(missing_ok=True)
    if memory_path.is_dir():
        shutil.rmtree(memory_path)
    edit.publish([".foragent.yml", agent, memory], f"Foragent role {name} removed")
