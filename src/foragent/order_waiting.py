"""An issue owns its wait: the `## Waiting` section of its body, kept as written.

The section's text (reason, next condition, responsible role, or anything the person
wrote) is not interpreted here; agents read it through `fa order context`. Publication
leaves one comment line, in the configured language, when the issue's wait changes, and
none when it stays the same.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from foragent import i18n

HEADING = re.compile(r"^##\s+Waiting\s*$", re.MULTILINE)
_END = re.compile(r"^(?:#{1,2}\s|<!--\s*foragent:)", re.MULTILINE)
_MARKER = re.compile(r"<!-- foragent:waiting from=[0-9a-f]{64} sha256=[0-9a-f]{64} id=([0-9a-f]{32}) -->")
_PRIOR = re.compile(r"<!-- foragent:waiting-prior sha256=([0-9a-f]{64}) id=([0-9a-f]{32}) -->\n?")
_WRITE = re.compile(r"<!-- foragent:write id=([0-9a-f]{32}) -->\n?")


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


def _prior(body: str | None) -> tuple[str, str] | None:
    match = _PRIOR.search(body or "")
    return (match[1], match[2]) if match else None


def stamp(before: str | None, after: str) -> str:
    """`after` as it is written over `before`, carrying the wait change it makes.

    A transition is what this write observes: the wait in `before` (the issue as read
    now, hand edits included) differs from the wait in `after`. Each such change gets a
    fresh id written with the body itself, so repeating A→B after someone restored A by
    hand is a new change, and a comment lost after the write is still owed on retry.
    An unchanged section keeps `before`'s record; bodies whose wait never changed get
    nothing, so they stay byte for byte the same.
    """
    after = _PRIOR.sub("", after)
    old, new = section(before), section(after)
    if old != new:
        prior, ident = _digest(old), uuid.uuid4().hex
    else:
        prior, ident = _prior(before) or (_digest(old), "")
    if prior == _digest(new):
        return after
    return f"{after}<!-- foragent:waiting-prior sha256={prior} id={ident} -->\n"


def mark_write(before: str, after: str) -> str:
    """`after` with a write id that is new whenever it differs from `before`, else carried.

    A record keyed by the id (the edit revision comment) then names this write, not an
    earlier identical one: rewriting a body back over a hand edit is a new write.
    """
    after = _WRITE.sub("", after)
    kept = _WRITE.search(before or "")
    if _WRITE.sub("", before or "") == after:
        return f"{after}<!-- foragent:write id={kept[1]} -->\n" if kept else after
    return f"{after}<!-- foragent:write id={uuid.uuid4().hex} -->\n"


def comment_line(prior: str, ident: str, text: str | None, language: str = "en") -> str:
    """One line in the configured language: the section's lines joined, or a cleared note,
    then the hidden marker of the change it announces. The authored text is verbatim."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if text is None:
        shown = i18n.text("waiting_cleared", language)
    else:
        shown = i18n.text("waiting_line", language, text=" · ".join(lines) or i18n.text("waiting_empty", language))
    return f"{shown} <!-- foragent:waiting from={prior} sha256={_digest(text)} id={ident} -->"


def announce(tracker, number: int, body: str, language: str = "en") -> None:
    """Comment once for the wait change the written `body` records; call it after the write.

    The body (from `stamp`) names the change with its own id; a line is owed until a
    waiting comment with that exact id exists. So a retry after a lost comment posts it,
    a repeat stays quiet, and an identical-looking later change still comments.
    """
    recorded = _prior(body)
    if recorded is None:
        return
    prior, ident = recorded
    if any(match[1] == ident for comment in tracker.list_comments(number)
           for match in _MARKER.finditer(comment.body)):
        return
    tracker.comment(number, comment_line(prior, ident, section(body), language))
