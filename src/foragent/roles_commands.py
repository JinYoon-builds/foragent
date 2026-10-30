"""fa role add, set and remove: write the roles source and its derived files in one command (#769)."""

from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import yaml

from foragent import role_agents
from foragent.config import load_config
from foragent.config_validation import _validate
from foragent.git import GitError, GitRepo
from foragent.paths import repo_path
from foragent.roles import (
    DEFAULT_ROLES, STATE_NAMES, RoleError, _agent_text, _description, _ensure_memory, _memory_file, _now,
    _split_agent, edit_roles, memory_dir, parse_signals, report_dropped_comments, with_defaults,
)


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
        written = deepcopy((yaml.safe_load(self.text) or {}).get("roles") or {})
        # A config from before roles existed has no block; the first role command writes it
        # starting from the default entries (fa init leaves such a file unchanged).
        self.roles = with_defaults(self.root, written) or written
        # Default entries this command adds to the source; save derives their files too.
        self.inserted = [name for name in self.roles if name not in written]
        self.derived: list[str] = []

    def check_branch(self, verb: str) -> None:
        if self.git.current_branch() != self.config.base_branch:
            raise GitError(f"fa role {verb} must run on the base branch {self.config.base_branch!r}")

    def check_skills(self, skills: list[str]) -> list[str]:
        """Installed skill names; each resolves as a repo product skill, a library skill or an installed copy.

        Skills that still need copying are kept in ``self.to_install``, like the lanes code copies them.
        """
        from foragent.skills import SkillError, SkillManager, with_vendored_library

        self.to_install = []
        names = []
        for identity in skills:
            if self.manager is None:
                self.manager = SkillManager(with_vendored_library(self.config, Path.cwd(), self.root))
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
        report_dropped_comments(self.text)
        self.derive_inserted(roles)

    def derive_inserted(self, roles: dict[str, Any]) -> None:
        """Create the agent, memory and skill copies of default entries this command added to the source.

        Every entry the source declares gets its projection in the same command; existing files are kept.
        """
        inserted = {name: roles[name] for name in self.inserted if name in roles}
        if not inserted:
            return
        skills = {entry.get("agent") or f"fa-{name}": entry.get("skills", []) for name, entry in inserted.items()}
        files, _ = role_agents.missing_files(self.root, skills)
        prefixes = tuple(f".claude/{kind}/{agent}" for agent in skills for kind in ("agents", "agent-memory"))
        prefixes += tuple(f".claude/skills/{skill}/" for names in skills.values() for skill in names)
        for path, data in files.items():
            if not path.startswith(prefixes):
                continue
            target = repo_path(self.root, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
            self.derived.append(path)

    def publish(self, owned: list[str], title: str) -> list[str]:
        """Commit the whole current state of the paths a role owns, against HEAD.

        ``owned`` is the role's settings file, agent file and memory directory (plus
        .gitignore for new skill copies). Edits and deletions made by hand before the
        command are staged too (``git add -A``), not only the files this command wrote.
        Nothing to commit is no error: the orch event never depends on a commit.
        """
        owned = [*owned, *(path for path in self.derived if path not in owned)]
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
    header, body, newline = _split_agent(agent_path)
    header = {"name": entry.get("agent") or f"fa-{name}", "memory": "project", **(header or {}),
              "description": _description(name), "skills": entry.get("skills", [])}
    if not agent_path.exists():
        # Only a missing agent gets the generated body; an existing one, even empty, keeps its own.
        body = _agent_text(name, header["skills"]).split("---\n", 2)[2].encode("utf-8")
    memory = _memory_file(edit.root, name, entry.get("memory") or memory_dir(name))
    owned = [".foragent.yml", agent, memory.removesuffix("/MEMORY.md"), *([".gitignore"] if edit.to_install else [])]
    edit.save({**edit.roles, name: entry})
    _ensure_memory(edit.root, name, entry)
    destination = repo_path(edit.root, agent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Only the frontmatter is regenerated, in the file's own line end; the body keeps its bytes.
    front = yaml.safe_dump(header, sort_keys=False, allow_unicode=True).replace("\n", newline)
    destination.write_bytes(f"---{newline}{front}---{newline}".encode("utf-8") + body)
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
