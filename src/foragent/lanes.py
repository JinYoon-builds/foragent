"""Manage lanes' thin agent definitions and repository configuration."""

from dataclasses import asdict
from pathlib import Path
import re

import yaml

from foragent import lane_memory
from foragent.config import Config, LaneSettings, _validate, load_config
from foragent.git import GitError, GitRepo
from foragent.paths import repo_path
from foragent.skills import SkillManager, with_vendored_library


class LaneError(ValueError):
    """A lane cannot be created or updated with the requested settings."""


def _check_skills(root: Path, skills: list[str], manager: SkillManager | None) -> list[str]:
    selected = [manager.check_install(identity, root) for identity in skills]
    installed = [skill.path.name for skill in selected]
    if len(set(installed)) != len(installed):
        raise LaneError("duplicate lane skills or conflicting installation names")
    if sum(skill.role == "style" for skill in selected) > 1:
        raise LaneError("a lane can have at most one style skill")
    return installed


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
    manager = manager or (SkillManager(with_vendored_library(config, Path.cwd(), root)) if skills else None)
    return _check_skills(root, skills, manager)


def lane_files(root: Path, name: str, skills: list[str]) -> list[str]:
    paths = [".foragent.yml", f".claude/agents/fa-{name}.md"]
    for skill in skills:
        directory = repo_path(root, f".claude/skills/{skill}")
        if not (directory / "SKILL.md").is_file():
            raise LaneError(f"installed lane skill is missing: {skill}")
    if skills:
        paths.append(".gitignore")
    if repo_path(root, lane_memory.ATTRIBUTES).is_file():
        paths.append(lane_memory.ATTRIBUTES)  # add_lane installs the pending-memory union line.
    return paths


def agent_text(name: str, paths: list[str], installed: list[str]) -> str:
    """The lane agent definition add_lane writes; order preparation shows a proposed lane the same file."""
    areas = ", ".join(paths)
    # Keep the agent definition readable and its skill list inline.
    header = yaml.safe_dump({
        "name": f"fa-{name}", "description": f'Foragent lane "{name}": implements issues in {areas}.',
    }, sort_keys=False) + "skills: " + yaml.safe_dump(installed, default_flow_style=True).strip() + "\n"
    return (f"---\n{header}---\n"
            f"You implement one GitHub issue at a time in this repository's {name} lane ({areas}).\n"
            f"{lane_memory.instruction(f'fa-{name}')}\n"
            "Run tests with `fa test <pytest args>`; it waits for a machine-wide test slot.\n")


def add_lane(root: Path, name: str, paths: list[str], *, skills: list[str] | None = None,
             executor: str | None = None, model: str | None = None,
             git: GitRepo | None = None, manager: SkillManager | None = None,
             push: bool = True, title: str | None = None, emoji: str | None = None) -> dict:
    config = load_config(root)
    skills = skills or []
    manager = manager or (SkillManager(with_vendored_library(config, Path.cwd(), root)) if skills else None)
    installed = validate_lane(root, config, name, paths, skills, executor, model, manager)
    git = git or GitRepo(root)
    if git.current_branch() != config.base_branch:
        raise GitError(f"lanes add must run on the base branch {config.base_branch!r}")
    lane = {"paths": paths, "agent": f"fa-{name}", "skills": installed}
    if executor is not None:
        lane["executor"] = executor
    if model is not None:
        lane["model"] = model
    for key, value in (("title", title), ("emoji", emoji)):
        if value is not None:
            lane[key] = value
    _validate({**asdict(config), "lanes": {**config.lanes, name: lane}})
    settings = LaneSettings(repo_path(root, ".foragent.yml"))
    settings.add_lane(name, lane, config.lanes)
    settings.check()
    for identity in skills:
        manager.install(identity, root)
    settings.save()
    agent_path = f".claude/agents/fa-{name}.md"
    destination = repo_path(root, agent_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(agent_text(name, paths, installed), encoding="utf-8")
    if (attributes := lane_memory.union_change(root, load_config(root))) is not None:
        lane_memory.apply(root, {lane_memory.ATTRIBUTES: attributes})
    published = lane_files(root, name, installed)
    git.ensure_tracked(published)
    git.commit_paths(published, f"Foragent lane {name}")
    if push:
        git.push_base(config.base_branch)
    return lane


def set_lane_skills(root: Path, name: str, skills: list[str], *,
                    manager: SkillManager | None = None) -> dict:
    """Replace an existing lane's fixed skills, preserving its other settings."""
    config = load_config(root)
    if name not in config.lanes:
        raise LaneError(f"unknown lane: {name}")
    lane = {**config.lanes[name]}
    agent_name = lane.get("agent") or f"fa-{name}"
    agent = repo_path(root, f".claude/agents/{agent_name}.md")
    try:
        content = agent.read_text(encoding="utf-8")
    except OSError as exc:
        raise LaneError(f"cannot read lane agent {agent}: {exc}") from exc
    match = re.match(r"\A\ufeff?---[ \t]*\r?\n(.*?)^---[ \t]*\r?(?:\n|$)", content, re.S | re.M)
    try:
        header = yaml.safe_load(match[1]) if match else None
    except yaml.YAMLError as exc:
        raise LaneError(f"invalid agent frontmatter: {agent}: {exc}") from exc
    if not isinstance(header, dict):
        raise LaneError(f"agent requires mapping frontmatter: {agent}")
    manager = manager or (SkillManager(with_vendored_library(config, Path.cwd(), root)) if skills else None)
    installed = _check_skills(root, skills, manager)
    lane["skills"] = installed
    _validate({**asdict(config), "lanes": {**config.lanes, name: lane}})
    settings = LaneSettings(repo_path(root, ".foragent.yml"))
    settings.set_skills(name, installed, lane, config.lanes)
    settings.check()
    header["skills"] = installed
    agent_content = ("---\n" + yaml.safe_dump(header, sort_keys=False, allow_unicode=True)
                     + "---\n" + content[match.end():])
    for identity in skills:
        manager.install(identity, root)
    settings.save()
    agent.write_text(agent_content, encoding="utf-8")
    return lane


def talk(number: int, *, message: str | None = None, store, config: Config, repo_root: Path | None = None,
         accepted=None) -> str:
    """Open issue ``number``'s live log or resume terminal (``fa lanes talk``); return the line to print."""
    from foragent import lane_talk_open

    return lane_talk_open.talk(number, message=message, store=store, config=config, repo_root=repo_root,
                               accepted=accepted)
