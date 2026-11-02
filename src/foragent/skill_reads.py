"""Which skills an executor really read, from its own log (#1619).

A read counts only when a tool call named a ``skills/<name>/SKILL.md``, succeeded, and its
output carries that file's opening lines. The output is the evidence: a shell command's shape
cannot prove a read (a skipped branch, a redirection, an early exit or a quoted operator all
look alike), but the file's own text in the result can. A final report, a failed call or a
path in prose establishes nothing. This module is the only writer of ``Run.skills_used``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
from threading import Lock
from typing import TYPE_CHECKING, Any

from foragent.executors import _events

if TYPE_CHECKING:
    from foragent.state import StateStore

_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
# Path-like words ending in SKILL.md; quoting and shell syntax around them do not matter.
_MENTION = re.compile(r"[^\s'\"`;|&<>()=]*SKILL\.md")
EVIDENCE_LINES = 3


def _skill_name(path: str) -> str | None:
    """The folder of ``.../skills/<name>/SKILL.md``; another file, a glob or a variable is no skill."""
    parts = PurePosixPath(path).parts
    if len(parts) >= 3 and parts[-1] == "SKILL.md" and parts[-3] == "skills" and _NAME.fullmatch(parts[-2]):
        return parts[-2]
    return None


def _shown(path: Path, output: str) -> bool:
    """Whether ``output`` carries the file's first non-blank lines, each on its own.

    Line by line, so `cat -n` and Claude's numbered Read output still match.
    """
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError):
        return False
    return bool(lines) and all(line in output for line in lines[:EVIDENCE_LINES])


def _reads(candidates: list[str], output: Any, worktree: Path | None) -> list[str]:
    """The named skills among ``candidates`` whose file text the output shows."""
    if not isinstance(output, str) or not output:
        return []
    read = []
    for candidate in candidates:
        name = _skill_name(candidate)
        if name is None:
            continue
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            if worktree is None:
                continue
            path = worktree / path
        if _shown(path, output):
            read.append(name)
    return read


def _text(content: Any) -> str:
    """A tool result's text, whether one string or a list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content
                         if isinstance(block, dict) and isinstance(block.get("text"), str))
    return ""


def _candidates(block: dict) -> list[str]:
    """The SKILL.md paths a Claude tool call names; its result decides whether it read them."""
    tool, values = block.get("name"), block.get("input")
    if not isinstance(values, dict):
        return []
    if tool == "Read" and isinstance(values.get("file_path"), str):
        return [values["file_path"]]
    if tool == "Bash" and isinstance(values.get("command"), str):
        return _MENTION.findall(values["command"])
    return []


def skills_read(log: Path, offset: int = 0, worktree: Path | None = None) -> list[str]:
    """Each skill the log shows a successful read of, once, in the order first read.

    Relative paths are the executor's, so they resolve against its ``worktree``.
    """
    read, waiting = [], {}
    for event in _events(log, offset=offset):
        item = event.get("item")
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "command_execution" and item.get("exit_code") == 0
                and item.get("status", "completed") == "completed" and isinstance(item.get("command"), str)):
            read.extend(_reads(_MENTION.findall(item["command"]), item.get("aggregated_output"), worktree))
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and event.get("type") == "assistant" and block.get("id"):
                waiting[block["id"]] = _candidates(block)
            elif block.get("type") == "tool_result" and event.get("type") == "user" and block.get("tool_use_id"):
                candidates = waiting.pop(block["tool_use_id"], [])
                if not block.get("is_error"):
                    read.extend(_reads(candidates, _text(block.get("content")), worktree))
    return list(dict.fromkeys(read))


_writing = Lock()  # A review panel's reviewers finish at the same time; each adds to one list.


def record(store: StateStore, n: int, log: Path, offset: int, worktree: Path | None = None) -> None:
    """Add what this session's part of the log shows; the session's own result is unaffected."""
    try:
        names = skills_read(log, offset, worktree) if log.exists() else []
    except OSError:
        return
    if not names:
        return
    with _writing:
        run = store.get_run(n)
        if run is not None:
            store.update_run(n, skills_reported=True, skills_used=list(dict.fromkeys([*run.skills_used, *names])))
