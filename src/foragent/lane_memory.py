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


def memory_dir(config: Config, lane: str) -> str:
    """The one memory directory name every writer and reader of the lane uses.

    The lane's agent when it sets one (for example fa-orch), else the lane name; the agent's
    append instruction, the executors' notes, the lane view and the dream all resolve it here.
    """
    return (config.lanes.get(lane) or {}).get("agent") or lane


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
    whose own YAML is ``memory: project`` (in any spelling) is removed. A header YAML cannot be
    split this way (a flow mapping) is written again from its data.
    """
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
    if _native_header(result):
        data = _load(header)
        newline = "\r\n" if "\r\n" in header else "\n"
        data.pop("memory", None)
        result = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).replace("\n", newline) if data else ""
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


_CURATED_SAMPLE = f".claude/agent-memory/fa-lane/{CURATED}"
_PENDING_SAMPLE = f".claude/agent-memory/fa-lane/{PENDING}"
# Paths a line about lane curated memory does not cover; a line matching one of them is broader.
_OTHER_SAMPLES = (_PENDING_SAMPLE, f"docs/{CURATED}", f"{CURATED}", ".claude/agent-memory/fa-lane/notes.md")
CURATED_DEFAULT = f".claude/agent-memory/**/{CURATED} !merge"
CURATED_DEFAULT_COMMENT = "# Only the lane's dream edits curated memory; it merges as ordinary text."


def _pattern_matches(pattern: str, path: str) -> bool:
    """Git's gitattributes pattern rules, for a root .gitattributes and a file path."""
    if pattern.startswith('"') and pattern.endswith('"') and len(pattern) > 1:
        pattern = pattern[1:-1]
    if "/" not in pattern.rstrip("/"):  # No slash: it matches the file name at any depth.
        return re.fullmatch(_glob(pattern), path.rsplit("/", 1)[-1]) is not None
    return re.fullmatch(_glob(pattern.lstrip("/")), path) is not None


def _glob(pattern: str) -> str:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out, i = out + "/.*", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        elif pattern[i] == "[":
            end = pattern.find("]", i + 1)
            if end < 0:
                out, i = out + re.escape("["), i + 1
            else:
                body = pattern[i + 1:end].replace("\\", "\\\\")
                out, i = out + "[" + ("^" + body[1:] if body.startswith("!") else body) + "]", end + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return out


def _split(line: str) -> tuple[str, list[str]] | None:
    """A gitattributes line's pattern and attribute tokens; None for blanks, comments and macros."""
    body = line.strip()
    if not body or body.startswith("#") or body.startswith("[attr]"):
        return None
    if body.startswith('"'):
        end = body.find('"', 1)
        pattern, rest = (body[:end + 1], body[end + 1:]) if end > 0 else (body, "")
    else:
        pattern, _, rest = re.match(r"(\S+)(\s*)(.*)", body).groups()
    return pattern, rest.split()


def _merge_value(lines: list[str], path: str) -> str | None:
    """The merge attribute Git applies to ``path``: the last matching line that sets it wins."""
    value = None
    for line in lines:
        parsed = _split(line)
        if parsed is None or not _pattern_matches(parsed[0], path):
            continue
        for token in parsed[1]:
            name = token.lstrip("-!").split("=", 1)[0]
            if name == "merge":
                value = (token.split("=", 1)[1] if "=" in token else
                         "unset" if token[0] == "-" else None if token[0] == "!" else "set")
    return value


def migrate_attributes(text: str | None) -> str:
    """Union merges move from the curated file to the pending file.

    Lines are read with Git's rules, so any spelling counts (pattern form, spacing, other
    attributes). A line only about lane MEMORY.md files loses its ``merge=union`` and keeps its
    other attributes; a broader line (for example ``MEMORY.md merge=union``, which also covers
    files outside lane memory) stays, and a later ``!merge`` line restores the default merge for
    lane MEMORY.md files. The pending union line is added unless MEMORY.new.md already merges by
    union.
    """
    lines = (text or "").splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in (text or "") else "\n"
    kept = []
    for line in lines:
        parsed = _split(line)
        specific = (parsed is not None and _pattern_matches(parsed[0], _CURATED_SAMPLE)
                    and not any(_pattern_matches(parsed[0], other) for other in _OTHER_SAMPLES))
        if not specific or "merge=union" not in parsed[1]:
            kept.append(line)
            continue
        attributes = [token for token in parsed[1] if token != "merge=union"]
        ending = line[len(line.rstrip("\r\n")):]
        if attributes:
            kept.append(" ".join([parsed[0], *attributes]) + ending)
        elif line.strip() == OLD_UNION and not any(other.strip() == UNION for other in lines + kept):
            kept.append(UNION + ending)  # The old line becomes the new one in place.
    result = "".join(kept)

    def add(*added: str) -> None:
        nonlocal result
        if result and not result.endswith("\n"):
            result += newline
        result += "".join(line + newline for line in added)

    if _merge_value(result.splitlines(), _CURATED_SAMPLE) == "union":
        add(CURATED_DEFAULT_COMMENT, CURATED_DEFAULT)
    if _merge_value(result.splitlines(), _PENDING_SAMPLE) != "union":
        add(UNION_COMMENT, UNION)
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
