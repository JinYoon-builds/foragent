"""Create a lane's thin agent definition and repository configuration."""

from dataclasses import asdict
from pathlib import Path
import re

import yaml

from foragent.config import Config, _read_yaml, _validate, load_config
from foragent.git import GitError, GitRepo
from foragent.paths import repo_path
from foragent.skills import SkillManager


class LaneError(ValueError):
    """A lane cannot be added without overwriting existing settings."""


def validate_lane(root: Path, config: Config, name: str, paths: list[str], skills: list[str],
                  executor: str | None = None, model: str | None = None,
                  manager: SkillManager | None = None) -> list[str]:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]+", name):
        raise LaneError(f"invalid lane name: {name}")
    if name in config.lanes:
        raise LaneError(f"lane already exists: {name}")
    if not paths or any(not isinstance(path, str) or not path.strip() for path in paths):
        raise LaneError("lane paths must be a nonempty list of globs")
    if executor is not None and executor not in {"codex", "claude"}:
        raise LaneError("lane executor must be codex or claude")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise LaneError("lane model must be a nonempty string")
    agent = repo_path(root, f".claude/agents/fa-{name}.md")
    if agent.exists():
        raise LaneError(f"agent already exists: {agent.relative_to(root)}")
    repo_path(root, ".foragent.yml")
    if len(set(skills)) != len(skills):
        raise LaneError("duplicate lane skills")
    manager = manager or (SkillManager(config) if skills else None)
    return [manager.check_install(identity, root).path.name for identity in skills]


def lane_files(root: Path, name: str, skills: list[str]) -> list[str]:
    paths = [".foragent.yml", f".claude/agents/fa-{name}.md"]
    for skill in skills:
        directory = repo_path(root, f".claude/skills/{skill}")
        if not (directory / "SKILL.md").is_file():
            raise LaneError(f"installed lane skill is missing: {skill}")
    if skills:
        paths.append(".gitignore")
    return paths


def add_lane(root: Path, name: str, paths: list[str], *, skills: list[str] | None = None,
             executor: str | None = None, model: str | None = None,
             git: GitRepo | None = None, manager: SkillManager | None = None,
             push: bool = True, title: str | None = None, emoji: str | None = None) -> dict:
    config = load_config(root)
    skills = skills or []
    manager = manager or (SkillManager(config) if skills else None)
    installed = validate_lane(root, config, name, paths, skills, executor, model, manager)
    git = git or GitRepo(root)
    if git.current_branch() != config.base_branch:
        raise GitError(f"lanes add must run on the base branch {config.base_branch!r}")
    for identity in skills:
        manager.install(identity, root)
    lane = {"paths": paths, "agent": f"fa-{name}", "skills": installed}
    if executor is not None:
        lane["executor"] = executor
    if model is not None:
        lane["model"] = model
    for key, value in (("title", title), ("emoji", emoji)):
        if value is not None:
            lane[key] = value
    local = _read_yaml(root / ".foragent.yml")
    local["lanes"] = {**config.lanes, name: lane}
    _validate({**asdict(config), "lanes": local["lanes"]})
    (root / ".foragent.yml").write_text(yaml.safe_dump(local, sort_keys=False, allow_unicode=True), encoding="utf-8")
    areas = ", ".join(paths)
    # Keep the agent definition readable and its skill list inline.
    header = yaml.safe_dump({
        "name": f"fa-{name}", "description": f'Foragent lane "{name}": implements issues in {areas}.',
        "memory": "project",
    }, sort_keys=False) + "skills: " + yaml.safe_dump(installed, default_flow_style=True).strip() + "\n"
    agent_path = f".claude/agents/fa-{name}.md"
    destination = repo_path(root, agent_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        f"---\n{header}---\n"
        f"You implement one GitHub issue at a time in this repository's {name} lane ({areas}).\n"
        "Record durable, project-specific lessons (patterns, pitfalls, commands) in your memory.\n",
        encoding="utf-8",
    )
    published = lane_files(root, name, installed)
    git.ensure_tracked(published)
    git.commit_paths(published, f"Foragent lane {name}")
    if push:
        git.push_base(config.base_branch)
    return lane
