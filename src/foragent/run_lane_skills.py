"""Project lane notes and installed skill context for the implementer."""

from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

import yaml

from foragent import cuts, lane_memory
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

    Memory is the curated MEMORY.md and the pending MEMORY.new.md (D95) in the lane's one memory
    directory (lane_memory.memory_dir); each keeps the 200-line limit, and a longer one ends with
    the cut notice, linking a full copy under ``full_text_dir`` (or the memory file itself).
    A lane without an agent file still gets its memory.
    """
    memory = _memory_sections(config, lane, worktree, full_text_dir, curated=True)
    agent, body = _agent_text(config, lane, worktree)
    if body is not None:
        body = re.sub(r"\A\ufeff?---\s*\n.*?\n---[^\S\n]*(?:\n|$)", "", body, count=1, flags=re.DOTALL)
    notes = ((body or "").strip() + _writer_contract(config, lane, body) + memory).strip()
    return agent, ("Lane notes:\n" + notes + "\n\n" if notes else "")


def _agent_text(config: Config, lane: str, worktree: Path) -> tuple[str | None, str | None]:
    """The lane's agent name and file text, or (None, None) when it has no readable agent file."""
    agent = _lane_settings(config, lane).get("agent")
    if not isinstance(agent, str) or not re.fullmatch(r"fa-[a-z0-9-]+", agent):
        return None, None
    path = worktree / ".claude/agents" / f"{agent}.md"
    return (agent, path.read_text(encoding="utf-8", errors="replace")) if path.is_file() else (None, None)


def _writer_contract(config: Config, lane: str, agent_text: str | None) -> str:
    """The append-only writer instruction, when the agent file does not already carry it.

    Every lane's executor gets the same contract (D95): lanes without an agent file, with a
    missing one, or with one not yet migrated by ``fa init`` get it here.
    """
    if _memory_folder(config, lane, Path(".")) is None:  # The same lanes whose memory is read.
        return ""
    contract = lane_memory.instruction(lane_memory.memory_dir(config, lane))
    return "" if agent_text is not None and contract in agent_text else "\n\n" + contract


def _memory_folder(config: Config, lane: str, worktree: Path) -> Path | None:
    if lane not in config.lanes:
        return None
    name = lane_memory.memory_dir(config, lane)
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        return None
    return worktree / lane_memory.directory(name)


def _memory_sections(config: Config, lane: str, worktree: Path, full_text_dir: Path | None, *, curated: bool) -> str:
    """The curated memory (when wanted) and the pending lessons, each cut at 200 lines."""
    folder = _memory_folder(config, lane, worktree)
    if folder is None:
        return ""
    sections = []
    for title, name, saved, wanted in (
        ("Memory", lane_memory.CURATED, "lane-memory.md", curated),
        ("Pending memory (MEMORY.new.md, not yet curated)", lane_memory.PENDING, "lane-memory-new.md", True),
    ):
        path = folder / name
        text = path.read_text(encoding="utf-8", errors="replace") if wanted and path.is_file() else ""
        if text.strip():
            cut = cuts.head_lines(text, 200, full_text_dir, saved, full=path.absolute())
            sections.append(f"\n\n{title}:\n{cut}")
    return "".join(sections)


# Claude loads only the start of an agent's MEMORY.md (first 200 lines or 25 KB, whichever comes first).
NATIVE_MEMORY_LINES = 200
NATIVE_MEMORY_BYTES = 25 * 1024


def lane_memory_notice(config: Config, lane: str, worktree: Path, full_text_dir: Path | None = None) -> str:
    """For Claude, which reads the lane agent itself: the lane memory it does not load natively.

    The pending lessons always come here. The curated memory comes here too, unless the agent
    still loads it natively (``memory: project``); then only its cut notice is added.
    """
    _, text = _agent_text(config, lane, worktree)
    loaded = text is not None and lane_memory.native(text)
    folder = _memory_folder(config, lane, worktree)
    notice = _native_cut_notice(folder, full_text_dir) if loaded and folder is not None else ""
    contract = _writer_contract(config, lane, text)
    sections = _memory_sections(config, lane, worktree, full_text_dir, curated=not loaded)
    body = contract + sections
    return notice + ("\n\nLane memory:" + body + "\n" if body else "")


def _native_cut_notice(folder: Path, full_text_dir: Path | None) -> str:
    memory = folder / lane_memory.CURATED
    if not memory.is_file():
        return ""
    text = memory.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    loaded = "".join(lines[:NATIVE_MEMORY_LINES])
    if len(loaded.encode("utf-8")) > NATIVE_MEMORY_BYTES:
        loaded = loaded.encode("utf-8")[:NATIVE_MEMORY_BYTES].decode("utf-8", errors="ignore")
        count, unit = len(text) - len(loaded), "chars"
    elif len(lines) > NATIVE_MEMORY_LINES:
        count, unit = len(lines) - NATIVE_MEMORY_LINES, "lines"
    else:
        return ""
    full = cuts.save_full(full_text_dir, "lane-memory.md", text) if full_text_dir is not None else memory.absolute()
    return ("\n\nYour lane memory is loaded only in part (first 200 lines or 25 KB). "
            + cuts.notice(count, unit, full) + "\n")


def _lane_skill_names(ctx: RunContext, n: int, worktree: Path, agent: str | None) -> list[str]:
    return lane_skill_names(ctx.config, ctx.store.get_run(n).lane, worktree, agent)


def lane_skill_names(config: Config, lane: str, worktree: Path, agent: str | None) -> list[str]:
    """The lane's skills from .foragent.yml, its single source (D95).

    The agent file's frontmatter is a projection the lanes code writes; an empty
    configured list means no skills, never the agent's list.
    """
    skills = _lane_settings(config, lane).get("skills", [])
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
