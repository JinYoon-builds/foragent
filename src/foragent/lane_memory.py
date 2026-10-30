"""Lane memory has two writers (D95): workers append MEMORY.new.md, the dream curates MEMORY.md.

Every reader (Codex lane notes, the Claude notice, the dream prompt, the lane view) shows both
files. ``migrate`` moves an existing project to this split; ``fa init`` calls it on every run, and
a second run changes nothing. Role agents keep their own memory contract and are never touched.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foragent.config import Config

CURATED = "MEMORY.md"
PENDING = "MEMORY.new.md"
ATTRIBUTES = ".gitattributes"
UNION = f".claude/agent-memory/**/{PENDING} merge=union"
OLD_UNION = f".claude/agent-memory/**/{CURATED} merge=union"
UNION_COMMENT = "# Pending lane lessons only gain lines; keep both sides when lane clones merge."
# The writer line lane agents had before the split; the migration replaces it.
OLD_WRITER = "Record durable, project-specific lessons (patterns, pitfalls, commands) in your memory."
# Agent files may start with a byte-order mark and use CRLF line ends; both stay as they are.
_FRONTMATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)^---[ \t]*\r?(?:\n|$)", re.S | re.M)
_NATIVE = re.compile(r"^memory:[ \t]*(?:project|'project'|\"project\")[ \t]*(?:#[^\r\n]*)?\r?\n", re.M)


def directory(agent: str) -> str:
    return f".claude/agent-memory/{agent}"


def instruction(agent: str) -> str:
    """The one writer instruction every lane executor reads, Claude and Codex alike."""
    folder = directory(agent)
    return (f"Append durable, project-specific lessons (patterns, pitfalls, commands) as new lines at the end of "
            f"`{folder}/{PENDING}`; create it if it is missing and never rewrite or remove earlier lines. "
            f"Do not edit `{folder}/{CURATED}`: the lane's dream alone curates it and folds "
            f"{PENDING} into it. Read both files before you start.")


def lane_agent(config: Config, lane: str) -> str | None:
    """The lane's agent file name; only an explicit ``agent`` setting names one (executors read no other)."""
    agent = (config.lanes.get(lane) or {}).get("agent")
    return agent if isinstance(agent, str) and re.fullmatch(r"fa-[a-z0-9-]+", agent) else None


def memory_dir(config: Config, lane: str) -> str:
    """The one memory directory name every writer and reader of the lane uses.

    The lane's agent when it sets one (for example fa-orch), else the lane name; the agent's
    append instruction, the executors' notes, the lane view and the dream all resolve it here.
    """
    return (config.lanes.get(lane) or {}).get("agent") or lane


def native(agent_text: str) -> bool:
    """Whether Claude loads the curated memory itself (an agent not yet migrated)."""
    match = _FRONTMATTER.match(agent_text)
    return bool(match and _NATIVE.search(match[1]))


def migrate_agent(text: str, agent: str) -> str:
    """The lane agent with native curated-write authority removed and the append instruction added.

    Only the managed lines change: other frontmatter keys and the body keep their bytes.
    """
    match = _FRONTMATTER.match(text)
    if match:
        header = _NATIVE.sub("", match[1])
        text = text[:match.start(1)] + header + text[match.end(1):]
    managed = instruction(agent)
    if managed in text:
        return text
    if OLD_WRITER in text:
        return text.replace(OLD_WRITER, managed, 1)
    newline = "\r\n" if "\r\n" in text else "\n"
    return text + ("" if text.endswith("\n") or not text else newline) + managed + newline


def migrate_attributes(text: str | None) -> str:
    """Union merges move from the curated file to the pending file."""
    lines = (text or "").splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in (text or "") else "\n"
    kept = []
    for line in lines:
        if line.strip() == OLD_UNION:
            if not any(other.strip() == UNION for other in lines + kept):
                kept.append(UNION + line[len(line.rstrip("\r\n")):])
        else:
            kept.append(line)
    result = "".join(kept)
    if not any(line.strip() == UNION for line in result.splitlines()):
        if result and not result.endswith("\n"):
            result += newline
        result += f"{UNION_COMMENT}{newline}{UNION}{newline}"
    return result


def union_change(root: Path) -> str | None:
    """The .gitattributes text with the pending-file union line, or None when it is already there.

    The one installer of the union merge for MEMORY.new.md: fresh init, re-init (migration) and
    every lane added later call it. The line is a glob, so it covers lanes with and without an
    agent alike.
    """
    path = Path(root) / ATTRIBUTES
    current = path.read_bytes().decode("utf-8") if path.is_file() else None
    new = migrate_attributes(current)
    return None if new == current else new


def _lane_agent_paths(root: Path, config: Config) -> list[str]:
    """The existing agent files of lanes with an explicit agent; role agents are never included."""
    roles = {(entry or {}).get("agent") or f"fa-{name}" for name, entry in (config.roles or {}).items()}
    paths = []
    for lane in config.lanes:
        agent = lane_agent(config, lane)
        if agent is not None and agent not in roles and (Path(root) / ".claude/agents" / f"{agent}.md").is_file():
            paths.append(f".claude/agents/{agent}.md")
    return list(dict.fromkeys(paths))


def managed_paths(root: Path, config: Config) -> list[str]:
    """Every file the split manages; init publishes any of them that differs from the commit.

    A rerun after an interrupted setup finds the files an earlier migration wrote by their state
    (uncommitted), not by whether they still need migrating.
    """
    return [*_lane_agent_paths(root, config), ATTRIBUTES]


def migrate(root: Path, config: Config) -> dict[str, str]:
    """Each file the split changes and its new text; empty once the project is migrated.

    Only lanes with an explicit agent have an agent file the executors read; agent files that do
    not exist are left absent, and role agents are skipped even when a lane names one.
    """
    root = Path(root)
    changes: dict[str, str] = {}
    for relative in _lane_agent_paths(root, config):
        agent = Path(relative).stem
        text = (root / relative).read_bytes().decode("utf-8")
        if (new := migrate_agent(text, agent)) != text:
            changes[relative] = new
    if (attributes := union_change(root)) is not None:
        changes[ATTRIBUTES] = attributes
    return changes


def apply(root: Path, changes: dict[str, str]) -> None:
    for relative, text in changes.items():
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
