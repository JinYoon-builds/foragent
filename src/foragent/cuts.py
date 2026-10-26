"""The shared cut notice: a size limit never silently drops text a model needs.

Every cut keeps its limit but says how much it left out and where the full text
is: ``[cut: N more chars|lines; full text: <absolute path>]``. Full copies live
under the issue's state folder, ``<state>/runs/issue-N/``.
"""

from __future__ import annotations

from pathlib import Path
import re

# How far a head cut may move back to end on a whole line.
LINE_SLACK = 200


def issue_dir(state_root: Path, n: int) -> Path:
    """Where an issue's full texts are saved."""
    return Path(state_root) / "runs" / f"issue-{n}"


def dir_for_log(log: Path) -> Path:
    """The full-text folder for a log: ``<state>/logs/issue-N.log`` maps to ``<state>/runs/issue-N``."""
    log = Path(log)
    if log.parent.name == "logs":
        return log.parent.parent / "runs" / log.stem
    return log.parent / f"{log.stem}-full"


def notice(count: int, unit: str, path: Path) -> str:
    return f"[cut: {count} more {unit}; full text: {Path(path).absolute()}]"


def save_full(directory: Path, name: str, text: str) -> Path:
    """Write ``text`` whole and return its absolute path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / (re.sub(r"[^A-Za-z0-9._-]+", "-", name) or "text.txt")).absolute()
    path.write_text(text, encoding="utf-8")
    return path


def head(text: str, limit: int, directory: Path | None, name: str) -> str:
    """The first ``limit`` characters, then the notice.

    The cut moves back to a line end only when one is near, so a long line
    never shrinks what is kept far below the limit.
    """
    if len(text) <= limit:
        return text
    end = text.rfind("\n", max(0, limit - LINE_SLACK), limit) + 1 or limit
    if directory is None:
        return text[:end]
    path = save_full(directory, name, text)
    return text[:end] + notice(len(text) - end, "chars", path)


def head_lines(text: str, limit: int, directory: Path | None, name: str, *, full: Path | None = None) -> str:
    """The first ``limit`` lines, then the notice; ``full`` names an existing whole copy."""
    lines = text.splitlines(keepends=True)
    if len(lines) <= limit:
        return text
    kept = "".join(lines[:limit])
    if not kept.endswith("\n"):
        kept += "\n"
    if directory is not None:
        full = save_full(directory, name, text)
    if full is None:
        return kept
    return kept + notice(len(lines) - limit, "lines", full)


def tail(text: str, limit: int, directory: Path | None, name: str) -> str:
    """The notice, then the last ``limit`` characters."""
    if len(text) <= limit:
        return text
    if directory is None:
        return text[-limit:]
    path = save_full(directory, name, text)
    return notice(len(text) - limit, "chars", path) + "\n" + text[-limit:]


def read_tail(log: Path, offset: int, limit: int, name: str) -> str:
    """The tail of what ``log`` gained after ``offset``, with a notice when it is longer."""
    log = Path(log)
    with log.open("rb") as stream:
        stream.seek(offset)
        output = stream.read().decode("utf-8", errors="replace")
    return tail(output, limit, dir_for_log(log), name)


FAILURES_FILE = "check-failures.txt"


def failures_path(state_root: Path, n: int) -> Path:
    """The latest verification's failing commands, each with its own tail; read by every new attempt."""
    return issue_dir(state_root, n) / FAILURES_FILE
