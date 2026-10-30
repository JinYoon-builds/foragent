"""Lane memory has one writer per file (D95).

Workers and people append lessons to MEMORY.new.md and never rewrite it. The dream alone curates
MEMORY.md and appends the ids it folded to MEMORY.consumed.ids. The pending lessons are derived
(pending_view): the buffer's lines whose id is not consumed. Every reader (Codex lane notes, the
Claude notice, the dream prompt, the lane view) shows the curated memory and the pending lessons. ``migrate`` moves an existing project to this split; ``fa init`` calls it on every run, and
a second run changes nothing. Role agents keep their own memory contract and are never touched.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from foragent.config import Config

CURATED = "MEMORY.md"
PENDING = "MEMORY.new.md"
# The ids of pending lessons the dream consumed; the dream is its one writer (D95).
CONSUMED = "MEMORY.consumed.ids"
ATTRIBUTES = ".gitattributes"
UNION = f".claude/agent-memory/**/{PENDING} merge=union"
OLD_UNION = f".claude/agent-memory/**/{CURATED} merge=union"
UNION_COMMENT = "# Pending lane lessons only gain lines; keep both sides when lane clones merge."
# The writer line lane agents had before the split; the migration replaces it.
OLD_WRITER = "Record durable, project-specific lessons (patterns, pitfalls, commands) in your memory."
# Agent files may start with a byte-order mark and use CRLF line ends; both stay as they are.
_FRONTMATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)^---[ \t]*\r?(?:\n|$)", re.S | re.M)


def directory(agent: str) -> str:
    return f".claude/agent-memory/{agent}"


def instruction(agent: str) -> str:
    """The one writer instruction every lane executor reads, Claude and Codex alike."""
    folder = directory(agent)
    return (f"Append durable, project-specific lessons (patterns, pitfalls, commands) at the end of "
            f"`{folder}/{PENDING}`, one line each as `- [id:<unique id>] <lesson>` (for example the issue "
            f"number, date and a short slug); create it if it is missing and never rewrite or remove any line. "
            f"Do not edit `{folder}/{CURATED}` or `{folder}/{CONSUMED}`: the lane's dream alone curates "
            f"{CURATED}, folding pending lessons into it and recording their ids in {CONSUMED}. "
            f"Before you start, read {CURATED} and the pending lessons: the lines of {PENDING} whose id is "
            f"not listed in {CONSUMED} (a listed id was already folded; an untagged line is its own id).")


def _instruction_v2(agent: str) -> str:
    """The writer instruction that named the pending lessons without their rule; migration replaces it."""
    folder = directory(agent)
    return (f"Append durable, project-specific lessons (patterns, pitfalls, commands) at the end of "
            f"`{folder}/{PENDING}`, one line each as `- [id:<unique id>] <lesson>` (for example the issue "
            f"number, date and a short slug); create it if it is missing and never rewrite or remove any line. "
            f"Do not edit `{folder}/{CURATED}` or `{folder}/{CONSUMED}`: the lane's dream alone curates "
            f"{CURATED}, folding pending lessons into it and recording their ids in {CONSUMED}. "
            f"Read {CURATED} and the pending lessons before you start.")


def _instruction_v1(agent: str) -> str:
    """The writer instruction before consumed ids (#1000, first version); migration replaces it."""
    folder = directory(agent)
    return (f"Append durable, project-specific lessons (patterns, pitfalls, commands) as new lines at the end of "
            f"`{folder}/{PENDING}`; create it if it is missing and never rewrite or remove earlier lines. "
            f"Do not edit `{folder}/{CURATED}`: the lane's dream alone curates it and folds "
            f"{PENDING} into it. Read both files before you start.")


_ID = re.compile(r"\[id:([^\]\s]+)\]")


def entry_id(line: str) -> str:
    """A pending line's id: its ``[id:...]`` tag, else the line's own text."""
    match = _ID.search(line)
    return match[1] if match else line.strip()


def consumed_ids(text: str | None) -> set[str]:
    """The ids the dream recorded, one per line."""
    return {line.strip() for line in (text or "").splitlines() if line.strip()}


def pending_view(pending: str | None, consumed: str | None) -> str:
    """The pending lessons: lines whose id the dream has not consumed, each tagged id once.

    Derived, never stored (D95): the buffer only gains lines, so a union merge, a rebase or a
    retried append cannot bring back a consumed lesson. Untagged lines (written before ids, or
    by a person) are their own id; they are kept as written, so a block of text is not reordered.
    """
    done = consumed_ids(consumed)
    seen: set[str] = set()
    kept = []
    for line in (pending or "").splitlines(keepends=True):
        if not line.strip():
            kept.append(line)
            continue
        key = entry_id(line)
        if key in done or (_ID.search(line) and key in seen):
            continue
        seen.add(key)
        kept.append(line)
    text = "".join(kept)
    return text if text.strip() else ""


def read_pending(folder: Path) -> str:
    """The pending lessons of a lane memory directory, derived from its two files."""
    def read(name: str) -> str | None:
        path = Path(folder) / name
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None

    return pending_view(read(PENDING), read(CONSUMED))


def lane_agent(config: Config, lane: str) -> str | None:
    """The lane's agent file name; only an explicit ``agent`` setting names one (executors read no other)."""
    agent = (config.lanes.get(lane) or {}).get("agent")
    return agent if isinstance(agent, str) and re.fullmatch(r"fa-[a-z0-9-]+", agent) else None


def memory_directory(name: str, agent: str | None = None, memory: str | None = None, *,
                     default: str | None = None) -> str:
    """The one resolver of a lane's or role's memory directory, relative and ending in ``/``.

    The explicit ``memory`` when set; else the agent's own directory, where Claude's native agent
    memory writes (``.claude/agent-memory/<agent>/``); else ``default``, which is ``fa-<name>``
    unless the caller names another. Roles, lanes, migration, the attribute writer and dream
    ownership all call this and nothing else derives a memory path. Lanes pass their own name as
    ``default`` so no lane memory moves (a lane has no ``memory`` key).
    """
    if isinstance(memory, str) and memory.strip().strip("/"):
        return memory.strip().rstrip("/") + "/"
    return f".claude/agent-memory/{agent or default or f'fa-{name}'}/"


def lane_directory(config: Config, lane: str) -> str:
    """The lane's memory directory (relative, no trailing ``/``): its agent's, else the lane name's."""
    agent = (config.lanes.get(lane) or {}).get("agent")
    return memory_directory(lane, agent if isinstance(agent, str) else None, default=lane).rstrip("/")


def memory_dir(config: Config, lane: str) -> str:
    """The lane's memory directory name (for example fa-orch), from lane_directory."""
    return Path(lane_directory(config, lane)).name


def _load(text: str):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _native_header(header: str) -> bool:
    data = _load(header)
    return isinstance(data, dict) and data.get("memory") == "project"


def native(agent_text: str) -> bool:
    """Whether Claude loads the curated memory itself (an agent not yet migrated).

    The frontmatter is read as YAML, so every spelling of ``memory: project`` counts.
    """
    match = _FRONTMATTER.match(agent_text)
    return bool(match and _native_header(match[1]))


def _without_native(header: str) -> str:
    """The frontmatter without its ``memory: project`` entry, other entries byte for byte.

    A top-level entry is a line at column 0 and the indented or blank lines after it; the entry
    whose own YAML is ``memory: project`` (in any spelling) is removed. The result must read as
    the same mapping without ``memory``; when it does not (a flow mapping, or an anchor the entry
    defined and another entry uses), the header is written again from its data.
    """
    if not _native_header(header):
        return header
    data = _load(header)
    expected = {key: value for key, value in data.items() if key != "memory"}
    lines = header.splitlines(keepends=True)
    entries: list[list[str]] = []
    for line in lines:
        starts = line[:1] not in {" ", "\t", "\r", "\n", "#", ""} and not line.startswith("-")
        if starts or not entries:
            entries.append([line])
        else:
            entries[-1].append(line)
    kept = [entry for entry in entries if _load("".join(entry)) != {"memory": "project"}]
    result = "".join("".join(entry) for entry in kept)
    if (_load(result) or {}) != expected:
        newline = "\r\n" if "\r\n" in header else "\n"
        result = yaml.safe_dump(expected, sort_keys=False, allow_unicode=True).replace("\n", newline) if expected else ""
    return result


def migrate_agent(text: str, agent: str) -> str:
    """The lane agent with native curated-write authority removed and the append instruction added.

    Only the managed lines change: other frontmatter keys and the body keep their bytes.
    """
    match = _FRONTMATTER.match(text)
    if match:
        header = _without_native(match[1])
        text = text[:match.start(1)] + header + text[match.end(1):]
    managed = instruction(agent)
    if managed in text:
        return text
    for old in (_instruction_v2(agent), _instruction_v1(agent), OLD_WRITER):
        if old in text:
            return text.replace(old, managed, 1)
    newline = "\r\n" if "\r\n" in text else "\n"
    return text + ("" if text.endswith("\n") or not text else newline) + managed + newline


# Comments earlier versions wrote above the union line; the managed block replaces them.
_OLD_COMMENTS = ("# Lane memory indexes only gain lines; keep both sides when lane clones merge.",
                 "# Only the lane's dream edits curated memory; it merges as ordinary text.",
                 "# Role memory keeps its own merge setting.")
CURATED_COMMENT = "# Only each lane's dream edits its curated memory; it merges as ordinary text."
# A lane's own curated-memory line; the managed block writes one per lane memory directory.
_LANE_CURATED = re.compile(r"\.claude/agent-memory/[A-Za-z0-9][A-Za-z0-9._-]*/" + re.escape(CURATED) + r" !merge")


def _lane_curated(name: str) -> str:
    return f"{directory(name)}/{CURATED} !merge"


def migrate_attributes(text: str | None, lanes: tuple[str, ...] = (), roles: tuple[str, ...] = ()) -> str:
    """The .gitattributes text with lane memory merging by union only on MEMORY.new.md.

    The one owner of lane-memory merge settings (D95): fresh init, re-init and ``fa lanes add``
    call it, and running it twice changes nothing. It never interprets other lines; it appends a
    managed block, which wins by Git's last-match rule whatever is written above it (any pattern,
    macro or driver): each lane's own MEMORY.md merges as ordinary text (``!merge``, one line per
    lane memory directory in ``lanes``) and every MEMORY.new.md by union. Role memory files are
    never named, so they keep their merge setting. The one line earlier foragent versions wrote,
    ``.claude/agent-memory/**/MEMORY.md merge=union``, is replaced in place by the same setting
    for each role memory file in ``roles``.
    """
    lines = (text or "").splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in (text or "") else "\n"
    managed = {UNION, UNION_COMMENT, CURATED_COMMENT, *_OLD_COMMENTS}
    kept = []
    own = {f"{directory(name)}/{file}" for name in lanes for file in (CURATED, PENDING)}
    for line in lines:
        body = line.strip()
        ending = line[len(line.rstrip("\r\n")):] or newline
        if body in managed or (_LANE_CURATED.fullmatch(body) and body.split()[0] not in roles):
            continue
        if body == OLD_UNION:
            kept.extend(f"{path} merge=union{ending}" for path in roles)
            continue
        fields = body.split()
        if fields and fields[0].strip('"').lstrip("/") in own:  # A line naming one lane memory file exactly.
            attributes = [token for token in fields[1:] if token.lstrip("-!").split("=", 1)[0] != "merge"]
            if attributes:
                kept.append(" ".join([fields[0], *attributes]) + ending)
            continue
        kept.append(line)
    result = "".join(kept)
    block = [CURATED_COMMENT, *(_lane_curated(name) for name in sorted(set(lanes))), UNION_COMMENT, UNION]
    if result and not result.endswith("\n"):
        result += newline
    return result + "".join(line + newline for line in block)


def _roles(config: Config) -> dict[str, dict]:
    """The roles as the roles module resolves them (defaults included without a roles block)."""
    from foragent import roles

    return roles.load_roles(config)


def role_agents(config: Config) -> set[str]:
    return {entry["agent"] for entry in _roles(config).values()}


def role_memory_files(config: Config) -> set[str]:
    """Each role's MEMORY.md path; role memory keeps its own writer and merge (#1000 out of scope)."""
    return {entry["memory"].rstrip("/") + f"/{CURATED}" for entry in _roles(config).values()}


def is_role_memory(config: Config, lane: str) -> bool:
    """Whether a lane's memory is a role's (by its agent or its directory): the role owns it."""
    return (lane_agent(config, lane) in role_agents(config)
            or f"{lane_directory(config, lane)}/{CURATED}" in role_memory_files(config))


def _memory_paths(config: Config) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The lanes' memory directory names (role memory excluded) and the role memory files."""
    lanes = tuple(sorted({memory_dir(config, lane) for lane in config.lanes
                          if not is_role_memory(config, lane)
                          and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(memory_dir(config, lane)))}))
    return lanes, tuple(sorted(role_memory_files(config)))


def union_change(root: Path, config: Config) -> str | None:
    """The .gitattributes text migrate_attributes gives, or None when it is already that.

    Fresh init, re-init (migration) and every lane added later call it.
    """
    path = Path(root) / ATTRIBUTES
    current = path.read_bytes().decode("utf-8") if path.is_file() else None
    new = migrate_attributes(current, *_memory_paths(config))
    return None if new == current else new


def _lane_agent_paths(root: Path, config: Config) -> list[str]:
    """The existing agent files of lanes with an explicit agent; role agents are never included."""
    paths = []
    for lane in config.lanes:
        agent = lane_agent(config, lane)
        if agent is not None and not is_role_memory(config, lane) and (Path(root) / ".claude/agents" / f"{agent}.md").is_file():
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
    if (attributes := union_change(root, config)) is not None:
        changes[ATTRIBUTES] = attributes
    return changes


def apply(root: Path, changes: dict[str, str]) -> None:
    for relative, text in changes.items():
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
