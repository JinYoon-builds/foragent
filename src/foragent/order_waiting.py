"""An issue owns its wait: the `## Waiting` section of its body, kept as written.

The section's text (reason, next condition, responsible role, or anything the person
wrote) is not interpreted here; agents read it through `fa order context`. Publication
leaves one comment line when the section changes, and none when it stays the same.
"""

from __future__ import annotations

import hashlib
import re

from foragent.inspect_data import comment_key

HEADING = re.compile(r"^##\s+Waiting\s*$", re.MULTILINE)
_END = re.compile(r"^(?:#{1,2}\s|<!--\s*foragent:)", re.MULTILINE)
_MARKER = re.compile(r"<!-- foragent:waiting sha256=([0-9a-f]{64}) -->")


def section(body: str | None) -> str | None:
    """The trimmed text under the first `## Waiting` heading, or None without one."""
    match = HEADING.search(body or "")
    if match is None:
        return None
    rest = body[match.end():]
    end = _END.search(rest)
    return (rest[:end.start()] if end else rest).strip()


def _digest(text: str | None) -> str:
    return hashlib.sha256(("" if text is None else "w:" + text).encode("utf-8")).hexdigest()


def comment_line(text: str | None) -> str:
    """One line: the section's lines joined, or a cleared note, with the state's hash."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    shown = " · ".join(lines) if text is not None else "cleared"
    return f"Waiting: {shown or '(empty)'} <!-- foragent:waiting sha256={_digest(text)} -->"


def announce(tracker, number: int, body: str) -> None:
    """Comment once when the issue's waiting section differs from the last one announced.

    The announced state is the latest waiting comment's hash (none means no wait), read
    from the issue itself, so a publication that stopped after writing the body but
    before commenting posts the missing line on retry, and a repeat stays quiet.
    """
    new = section(body)
    marked = [(comment, match[1]) for comment in tracker.list_comments(number)
              for match in _MARKER.finditer(comment.body)]
    latest = max(marked, key=lambda pair: comment_key(pair[0]))[1] if marked else _digest(None)
    if latest != _digest(new):
        tracker.comment(number, comment_line(new))
