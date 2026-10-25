"""Project lane notes and installed skill context for the implementer."""

from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

import yaml

from foragent import cuts
from foragent.git import GitRepo
from foragent.skills import SkillManager, copy_lane_skills

if TYPE_CHECKING:
    from foragent.config import Config
    from foragent.runner import RunContext


def _lane_settings(config: Config, lane: str) -> dict[str, Any]:
    return config.lanes.get(lane, {}) if lane != "main" else {}


def _lane_notes(ctx: RunContext, n: int, worktree: Path) -> tuple[str | None, str]:
    return lane_notes(ctx.config, ctx.store.get_run(n).lane, worktree)


def lane_notes(
    config: Config, lane: str, worktree: Path, full_text_dir: Path | None = None,
) -> tuple[str | None, str]:
    """The lane agent and its instructions and memory, as read from the worktree.

    Memory keeps its 200-line limit; a longer memory ends with the cut notice,
    linking a full copy under ``full_text_dir`` (or the memory file itself).
    """
    settings = _lane_settings(config, lane)
    agent = settings.get("agent")
    if not isinstance(agent, str) or not re.fullmatch(r"fa-[a-z0-9-]+", agent):
        return None, ""
    path = worktree / ".claude/agents" / f"{agent}.md"
    if not path.is_file():
        return None, ""
    body = path.read_text(encoding="utf-8", errors="replace")
    body = re.sub(r"\A---\s*\n.*?\n---[^\S\n]*(?:\n|$)", "", body, count=1, flags=re.DOTALL)
    memory = worktree / ".claude/agent-memory" / agent / "MEMORY.md"
    if memory.is_file():
        text = memory.read_text(encoding="utf-8", errors="replace")
        body += "\n\nMemory:\n" + cuts.head_lines(text, 200, full_text_dir, "lane-memory.md", full=memory.absolute())
    return agent, "Lane notes:\n" + body.strip() + "\n\n"


def _lane_skill_names(ctx: RunContext, n: int, worktree: Path, agent: str | None) -> list[str]:
    return lane_skill_names(ctx.config, ctx.store.get_run(n).lane, worktree, agent)


def lane_skill_names(config: Config, lane: str, worktree: Path, agent: str | None) -> list[str]:
    settings = _lane_settings(config, lane)
    skills = settings.get("skills", [])
    frontmatter = re.compile(r"\A---[^\S\n]*\n(.*?)^---[^\S\n]*(?:\n|$)", re.DOTALL | re.MULTILINE)
    if not skills and agent is not None:
        definition = (worktree / ".claude/agents" / f"{agent}.md").read_text(encoding="utf-8", errors="replace")
        match = frontmatter.match(definition)
        try:
            metadata = yaml.safe_load(match[1]) if match else None
        except yaml.YAMLError:
            metadata = None
        skills = metadata.get("skills", []) if isinstance(metadata, dict) else []
    return [name for name in skills if isinstance(name, str)] if isinstance(skills, list) else []


def _inject_lane_skills(ctx: RunContext, n: int, git: GitRepo, target_ref: str | None = None) -> None:
    target_ref = target_ref or f"origin/{ctx.config.base_branch}"
    git.local_skill_paths.update(ctx.store.get_run(n).local_skill_paths)
    git.reconcile_local_skills(target_ref)
    agent, _ = _lane_notes(ctx, n, git.root)
    names = _lane_skill_names(ctx, n, git.root, agent)
    names = [name for name in names if git.blob_sha(target_ref, f".claude/skills/{name}") is None]
    copied = copy_lane_skills(ctx.repo_root, git.root, names, SkillManager(ctx.config)) if names else []
    git.local_skill_paths.update(copied)
    ctx.store.update_run(n, local_skill_paths=sorted(git.local_skill_paths))


def _lane_skills(ctx: RunContext, n: int, worktree: Path, agent: str | None) -> tuple[str, list[str]]:
    return lane_skills(ctx.config, ctx.store.get_run(n).lane, worktree, agent)


def lane_skills(config: Config, lane: str, worktree: Path, agent: str | None) -> tuple[str, list[str]]:
    """List installed skills so Codex can read them when relevant."""
    skills = lane_skill_names(config, lane, worktree, agent)
    frontmatter = re.compile(r"\A---[^\S\n]*\n(.*?)^---[^\S\n]*(?:\n|$)", re.DOTALL | re.MULTILINE)
    sections, missing = [], []
    for name in skills:
        path = worktree / ".claude/skills" / name / "SKILL.md"
        if not path.resolve().is_relative_to(worktree.resolve()) or not path.is_file():
            missing.append(name)
            continue
        match = frontmatter.match(path.read_text(encoding="utf-8", errors="replace"))
        try:
            metadata = yaml.safe_load(match[1]) if match else None
        except yaml.YAMLError:
            metadata = None
        description = metadata.get("description", "") if isinstance(metadata, dict) else ""
        description = " ".join(description.split()) if isinstance(description, str) else ""
        sections.append(f"- {name}: {description} (.claude/skills/{name}/SKILL.md)")
    return ("Lane skills (read the file when relevant and follow it):\n" + "\n".join(sections) + "\n\n"
            if sections else ""), missing
