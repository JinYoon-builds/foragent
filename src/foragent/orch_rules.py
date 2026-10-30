"""Which document holds a project's current rules, read from the project's own folder."""

from __future__ import annotations

from pathlib import Path

RULES = "docs/rules.md"
HISTORY = "docs/decisions.md"


def current_rules(root: Path | None) -> tuple[str, str | None]:
    """(relative path, text) of docs/rules.md, else docs/decisions.md; text is None when both are missing."""
    if root is not None:
        for relative in (RULES, HISTORY):
            path = Path(root) / relative
            if path.is_file():
                return relative, path.read_text(encoding="utf-8", errors="replace")
    return HISTORY, None


def quoted(relative: str, content: str) -> str:
    """The document's text, framed so a reader sees where it starts and ends."""
    return f"--- {relative} ---\n{content.rstrip()}\n--- end of {relative} ---"
