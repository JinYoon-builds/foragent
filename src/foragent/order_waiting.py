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
from foragent.issue import closed_fence_lines

HEADING = re.compile(r"^##\s+Waiting\s*$", re.MULTILINE)
_END = re.compile(r"^(?:#{1,2}\s|<!--\s*foragent:)", re.MULTILINE)
_MARKER = re.compile(r"<!-- foragent:waiting from=[0-9a-f]{64} sha256=[0-9a-f]{64} id=([0-9a-f]{32}) -->")
_PRIOR = re.compile(r"<!-- foragent:waiting-prior sha256=([0-9a-f]{64}) id=([0-9a-f]{32}) -->")
_WRITE = re.compile(r"<!-- foragent:write id=([0-9a-f]{32}) -->")


def _is_marker(pattern: re.Pattern, line: str, fenced: bool) -> re.Match | None:
    """A marker the gate wrote fills its own line outside a closed fence; other text is the author's."""
    return None if fenced else pattern.fullmatch(line.rstrip("\r\n"))


def _marker(body: str | None, pattern: re.Pattern) -> re.Match | None:
    lines = (body or "").splitlines(keepends=True)
    fenced = closed_fence_lines(lines)
    return next((match for index, line in enumerate(lines)
                 if (match := _is_marker(pattern, line, index in fenced))), None)


def _without(body: str, pattern: re.Pattern) -> str:
    lines = body.splitlines(keepends=True)
    fenced = closed_fence_lines(lines)
    return "".join(line for index, line in enumerate(lines) if not _is_marker(pattern, line, index in fenced))


def _waiting_heading(body: str) -> re.Match | None:
    """The first `## Waiting` heading outside a closed fence: a fenced one is an authored example."""
    lines = body.splitlines(keepends=True)
    fenced = closed_fence_lines(lines)
    start = 0
    for index, line in enumerate(lines):
        if index not in fenced and (match := HEADING.match(body, start)):
            return match
        start += len(line)
    return None


def section(body: str | None) -> str | None:
    """The trimmed text under the first `## Waiting` heading, or None without one."""
    match = _waiting_heading(body or "")
    if match is None:
        return None
    rest = body[match.end():]
    end = _END.search(rest)
    return (rest[:end.start()] if end else rest).strip()


def read_waiting(body: str | None) -> dict[str, str] | None:
    """The current wait for projections, read through the canonical section reader.

    Keep the complete authored text. The ordinary three-field form additionally
    exposes its values for localized labels; other forms stay opaque. Publication's
    raw ``section`` contract (including an explicit ``None.``) remains unchanged.
    """
    raw = section(body)
    if raw is None or raw.strip() in {"", "None."}:
        return None
    fields = {"Reason": "reason", "Next condition": "next_condition", "Responsible role": "responsible_role"}
    values = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        label, separator, value = line.partition(":")
        if not separator or label not in fields or not value.strip() or fields[label] in values:
            return {"text": raw}
        values[fields[label]] = value.strip()
    return {"text": raw, **values} if len(values) == len(fields) else {"text": raw}


def replace_section(body: str, text: str | None) -> str:
    """Replace or clear this issue's wait, preserving everything outside its section."""
    replacement = f"## Waiting\n{text.strip()}\n\n" if text is not None else ""
    match = _waiting_heading(body)
    if match is None:
        return body + ("\n\n" if body else "") + replacement if replacement else body
    end = _END.search(body, match.end())
    return body[:match.start()] + replacement + (body[end.start():] if end else "")


def notification(body: str, language: str = "en") -> str:
    """The line owed by this body's stamped transition, for a caller's existing comment."""
    recorded = _prior(body)
    return comment_line(*recorded, section(body), language) if recorded is not None else ""


def _digest(text: str | None) -> str:
    return hashlib.sha256(("" if text is None else "w:" + text).encode("utf-8")).hexdigest()


def _prior(body: str | None) -> tuple[str, str] | None:
    match = _marker(body, _PRIOR)
    return (match[1], match[2]) if match else None


def on_own_line(body: str) -> str:
    """`body` ready for a marker appended after it: the marker gets a line of its own."""
    return body if not body or body.endswith("\n") else body + "\n"


def stamp(before: str | None, after: str) -> str:
    """`after` as it is written over `before`, carrying the wait change it makes.

    A transition is what this write observes: the wait in `before` (the issue as read
    now, hand edits included) differs from the wait in `after`. Each such change gets a
    fresh id written with the body itself, so repeating A→B after someone restored A by
    hand is a new change, and a comment lost after the write is still owed on retry.
    An unchanged section keeps `before`'s record; bodies whose wait never changed get
    nothing, so they stay byte for byte the same.
    """
    after = _without(after, _PRIOR)
    old, new = section(before), section(after)
    if old != new:
        prior, ident = _digest(old), uuid.uuid4().hex
    else:
        prior, ident = _prior(before) or (_digest(old), "")
    if prior == _digest(new):
        return after
    return f"{on_own_line(after)}<!-- foragent:waiting-prior sha256={prior} id={ident} -->\n"


def mark_write(before: str, after: str) -> str:
    """`after` with a write id that is new whenever it differs from `before`, else carried.

    A record keyed by the id (the edit revision comment) then names this write, not an
    earlier identical one: rewriting a body back over a hand edit is a new write.
    """
    after = _without(after, _WRITE)
    kept = _marker(before, _WRITE)
    if on_own_line(_without(before or "", _WRITE)) == on_own_line(after):
        return f"{on_own_line(after)}<!-- foragent:write id={kept[1]} -->\n" if kept else after
    return f"{on_own_line(after)}<!-- foragent:write id={uuid.uuid4().hex} -->\n"


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
