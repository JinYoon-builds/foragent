"""An issue owns its wait: the `## Waiting` section of its body, kept as written.

The section's text (reason, next condition, responsible role, or anything the person
wrote) is not interpreted here; agents read it through `fa order context`. Publication
leaves one comment line, in the configured language, when the issue's wait changes, and
none when it stays the same.
"""

from __future__ import annotations

import hashlib
import re

from foragent import i18n
from foragent.inspect_data import comment_key

HEADING = re.compile(r"^##\s+Waiting\s*$", re.MULTILINE)
_END = re.compile(r"^(?:#{1,2}\s|<!--\s*foragent:)", re.MULTILINE)
_MARKER = re.compile(r"<!-- foragent:waiting from=([0-9a-f]{64}) sha256=([0-9a-f]{64}) -->")
_PRIOR = re.compile(r"<!-- foragent:waiting-prior sha256=([0-9a-f]{64}) -->\n?")


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


def _prior(body: str | None) -> str | None:
    match = _PRIOR.search(body or "")
    return match[1] if match else None


def stamp(before: str | None, after: str) -> str:
    """`after` as it is written over `before`, carrying the wait it replaced.

    The written body records, with the write itself, the wait that held before the
    section last changed: a new change records `before`'s section, an unchanged section
    keeps `before`'s record. Without a pending transition (that wait equals the new one)
    nothing is added, so bodies that never had a wait change stay byte for byte the same.
    """
    after = _PRIOR.sub("", after)
    old, new = section(before), section(after)
    prior = _digest(old) if old != new else (_prior(before) or _digest(old))
    return after if prior == _digest(new) else f"{after}<!-- foragent:waiting-prior sha256={prior} -->\n"


def comment_line(prior: str, text: str | None, language: str = "en") -> str:
    """One line in the configured language: the section's lines joined, or a cleared note,
    then the hidden marker of the transition it announces. The authored text is verbatim."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if text is None:
        shown = i18n.text("waiting_cleared", language)
    else:
        shown = i18n.text("waiting_line", language, text=" · ".join(lines) or i18n.text("waiting_empty", language))
    return f"{shown} <!-- foragent:waiting from={prior} sha256={_digest(text)} -->"


def announce(tracker, number: int, body: str, language: str = "en") -> None:
    """Comment once for the wait transition the written `body` records; call it after the write.

    Both facts live on the issue: the body (from `stamp`) says which wait it replaced, and
    each waiting comment says which transition it announced. The line is posted unless the
    latest waiting comment already announces this exact transition, so a retry after a
    stop between the write and the comment posts the missing line (a cleared hand-written
    wait included), and a repeat stays quiet.
    """
    new = _digest(section(body))
    prior = _prior(body) or new
    if prior == new:
        return
    marked = [(comment, (match[1], match[2])) for comment in tracker.list_comments(number)
              for match in _MARKER.finditer(comment.body)]
    latest = max(marked, key=lambda pair: comment_key(pair[0]))[1] if marked else None
    if latest != (prior, new):
        tracker.comment(number, comment_line(prior, section(body), language))
