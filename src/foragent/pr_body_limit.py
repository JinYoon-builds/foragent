"""One budget rule for PR bodies: required content stays, each optional block is whole or a note."""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Self

PR_BODY_LIMIT = 65_536
COMMENT_LIMIT = 65_000

_MARKER = re.compile(r"<!-- (?P<close>/?)foragent:(?P<name>[^>]*?)\s*-->[ \t]*\r?\n?")
_PAIR_NAME = re.compile(r"[\w-]+")
_CLOSING = re.compile(r"^Closes #\d+[ \t\r]*(?:\n|$)", re.MULTILINE)
_REQUIRED_LINE = re.compile(
    r"^(?:(?P<heading>## (?:Review|Review scope|What changed|Summary|Needs a human check|Rebuttals|Searched"
    r"|Outside lane|Lane skills|Try it|Children))"
    r"|(?P<verdict>(?:Round \d+: )?VERDICT: \S+|Pending review\.|Disabled \(reviewer: none\)\.)"
    r"|(?P<gate>Opened by Foragent gate \(attempt \d+\)|Sub-issues of #\d+|#\d+의 하위 이슈))[ \t\r]*(?:\n|$)",
    re.MULTILINE,
)
_APPROVAL_HEADING = re.compile(r"^(#{1,6})[ \t]+Needs a human check[ \t\r]*(?:\n|$)", re.MULTILINE)
_HEADING = re.compile(r"^(#{1,6})[ \t]", re.MULTILINE)
_APPROVAL_LINE = re.compile(r"^[ \t]*[-*+][ \t]+\[[ xX]\].*(?:\n|$)", re.MULTILINE)
_SCOPE = re.compile(r"^## Review scope[ \t\r]*\n", re.MULTILINE)
_SUMMARY = re.compile(r"^## (?:What changed|Summary)[ \t\r]*\n", re.MULTILINE)
# Quoted text is masked with NUL before these run, so a section also stops at it.
_SECTION_STOP = re.compile(r"^#{1,6} |\0|<!-- /?foragent:", re.MULTILINE)
_PARAGRAPH = re.compile(r"\s*\S.*?(?:\n[ \t\r]*\n|\n?\Z)", re.DOTALL)
# The summary the body must keep is a short one; text past this is the implementer's prose.
_SUMMARY_LIMIT = 4_000
# The gate's scope rules are one paragraph; the limit only stops older prose counting as them.
_SCOPE_LIMIT = 8_000


class PrBodyTooLarge(RuntimeError):
    """Required content alone does not fit, so nothing may be sent."""


class Quoted(str):
    """Text in a rendered body that never counts as structure, whatever it looks like."""

    label: str

    def __new__(cls, text: str, label: str) -> Self:
        quoted = super().__new__(cls, text)
        quoted.label = label
        return quoted


Span = tuple[int, int, str]


class PrBody(str):
    """A body that knows its quoted spans, because the text alone cannot show them."""

    quoted: tuple[Span, ...]

    def __new__(cls, text: str, quoted: Iterable[Span] = ()) -> Self:
        body = super().__new__(cls, text)
        body.quoted = tuple(quoted)
        return body


def join_pr_body(parts: Iterable[str], separator: str = "") -> PrBody:
    """Join parts, keeping `Quoted` parts and the spans of nested bodies."""
    texts: list[str] = []
    quoted: list[Span] = []
    offset = 0
    for index, part in enumerate(parts):
        if index:
            texts.append(separator)
            offset += len(separator)
        if isinstance(part, Quoted) and part:
            quoted.append((offset, offset + len(part), part.label))
        quoted += [(offset + start, offset + end, label) for start, end, label in getattr(part, "quoted", ())]
        texts.append(part)
        offset += len(part)
    return PrBody("".join(texts), quoted)


def digest(body: str) -> str:
    return sha256(body.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _record_path(root: Path, pr: int) -> Path:
    return root / "pr-bodies" / f"quoted-{pr}.json"


def record_quoted(root: Path, pr: int, body: str) -> None:
    """Keep what is quoted in the body a PR now has: GitHub returns only its text."""
    if isinstance(body, PrBody):
        path = _record_path(root, pr)
        path.parent.mkdir(parents=True, exist_ok=True)
        quotes = [(body[start:end], label) for start, end, label in body.quoted]
        path.write_text(json.dumps(quotes), encoding="utf-8")


def load_quoted(root: Path, pr: int, body: str) -> PrBody:
    """The body with its quotes found again, so an edit elsewhere on GitHub keeps them known.

    A quote that is no longer there whole, or a PR sent before records existed, has no span.
    """
    try:
        quotes = json.loads(_record_path(root, pr).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        quotes = ()
    spans: list[Span] = []
    offset = 0
    for text, label in quotes:
        start = body.find(text, offset)
        if start >= 0:
            offset = start + len(text)
            spans.append((start, offset, label))
    return PrBody(body, spans)


def change_outside_quoted(body: PrBody, change: Callable[[str], str]) -> PrBody:
    """Apply a text edit that cannot see or match inside the quoted spans."""
    masked: list[str] = []
    offset = 0
    for index, (start, end, _) in enumerate(body.quoted):
        masked += [body[offset:start], f"\0{index}\0"]
        offset = end
    masked.append(body[offset:])
    parts = re.split(r"\0(\d+)\0", change("".join(masked)))
    return join_pr_body(
        Quoted(body[slice(*body.quoted[int(part)][:2])], body.quoted[int(part)][2]) if index % 2 else part
        for index, part in enumerate(parts)
    )


def _outer_pairs(markers: list[re.Match]) -> list[tuple[int, int, str]]:
    """Each complete pair not inside another, as (start, end, name); a closer takes its nearest opener."""
    open_by_name: dict[str, list[int]] = {}
    pairs: list[tuple[int, int, str]] = []
    for match in markers:
        name = match["name"]
        if not _PAIR_NAME.fullmatch(name):
            continue
        if not match["close"]:
            open_by_name.setdefault(name, []).append(match.start())
        elif open_by_name.get(name):
            pairs.append((open_by_name[name].pop(), match.start(), name))
    outer: list[tuple[int, int, str]] = []
    for pair in sorted(pairs):
        if not outer or pair[0] >= outer[-1][1]:
            outer.append(pair)
    return outer


@dataclass
class _Block:
    text: str
    label: str | None  # None is required content.
    quoted: bool = False


def short_section_end(text: str, start: int, limit: int = _SUMMARY_LIMIT) -> int:
    """Where a section's leading paragraphs end; a paragraph is whole or left out."""
    stop = _SECTION_STOP.search(text, start)
    section_end = stop.start() if stop else len(text)
    end = start
    for paragraph in _PARAGRAPH.finditer(text, start, section_end):
        if paragraph.end() - start > limit:
            break
        end = paragraph.end()
    return end


def _approval_spans(plain: str, outside: Callable[[int], bool]) -> list[tuple[int, int]]:
    """Each approval section's heading and lines outside any pair, read as the inbox reads them."""
    spans: list[tuple[int, int]] = []
    for heading in _APPROVAL_HEADING.finditer(plain):
        if not outside(heading.start()):
            continue
        spans.append(heading.span())
        level = len(heading[1])
        following = (match for match in _HEADING.finditer(plain, heading.end()) if len(match[1]) <= level)
        end = next((match.start() for match in following), len(plain))
        spans += [line.span() for line in _APPROVAL_LINE.finditer(plain, heading.end(), end) if outside(line.start())]
    return spans


def _blocks(body: str) -> list[_Block]:
    """Split a body into required pieces and the optional blocks between them."""
    quoted = getattr(body, "quoted", ())
    masked = list(body)
    for start, end, _ in quoted:
        masked[start:end] = "\0" * (end - start)
    plain = "".join(masked)

    markers = list(_MARKER.finditer(plain))
    pairs = _outer_pairs(markers)
    pair_starts = [pair[0] for pair in pairs]

    def enclosing(start: int) -> tuple[int, int, str] | None:
        index = bisect_right(pair_starts, start) - 1
        return pairs[index] if index >= 0 and start < pairs[index][1] else None

    # Text inside a pair is optional, whatever it says.
    def outside(start: int) -> bool:
        return enclosing(start) is None

    required = [match.span() for match in markers]
    required += [match.span() for match in _REQUIRED_LINE.finditer(plain) if outside(match.start())]
    closing = next((match for match in _CLOSING.finditer(plain) if outside(match.start())), None)
    if closing:
        required.append(closing.span())
    for pattern, limit in ((_SCOPE, _SCOPE_LIMIT), (_SUMMARY, _SUMMARY_LIMIT)):
        required += [(match.end(), short_section_end(plain, match.end(), limit))
                     for match in pattern.finditer(plain) if outside(match.start())]
    required += _approval_spans(plain, outside)

    def label_at(start: int) -> str:
        if pair := enclosing(start):
            return pair[2]
        heading = plain.rfind("\n## ", 0, start + 1)
        return plain[heading + 1:plain.find("\n", heading + 1)].removeprefix("## ").strip()[:60] if heading >= 0 else "PR text"

    blocks: list[_Block] = []
    offset = 0

    def add_optional(end: int) -> None:
        if offset < end:
            blocks.append(_Block(body[offset:end], label_at(offset)))

    events = sorted([(start, end, None) for start, end in required] + list(quoted), key=lambda event: event[0])
    for start, end, label in events:
        if start < offset:  # A required line that a wider required span already holds.
            start = offset
            if end <= offset:
                continue
        add_optional(start)
        blocks.append(_Block(body[start:end], label, quoted=label is not None))
        offset = end
    add_optional(len(body))
    return blocks


def fit_pr_body(body: str, note_for: Callable[[str], str], limit: int = PR_BODY_LIMIT) -> PrBody:
    """Return a body within the limit, or raise before anything required would be cut.

    Required content is set aside first, and blank space between blocks stays with it.
    Each optional block's share is what is left over the number of blocks, so no block's
    size can decide another block's fate: a block is whole or, past its share, one note.
    Length is Python characters, and a body that fits comes back unchanged.
    """
    if len(body) <= limit:
        return body if isinstance(body, PrBody) else PrBody(body)
    blocks = _blocks(body)
    optional = [block for block in blocks if block.label is not None and block.text.strip()]
    room = limit - sum(len(block.text) for block in blocks) + sum(len(block.text) for block in optional)
    if room < 0:
        raise PrBodyTooLarge(
            f"Required PR body content needs {limit - room} characters and the limit is {limit}: "
            "the closing reference, summary, verdict, approval lines and markers cannot be cut."
        )
    share = room // max(1, len(optional))
    for block in optional:
        if len(block.text) > share:
            note = note_for(block.label) + "\n"
            if len(note) < len(block.text):
                block.text, block.quoted = note, False
    size = sum(len(block.text) for block in blocks)
    if size > limit:
        raise PrBodyTooLarge(
            f"PR body needs {size} characters with its notes and the limit is {limit}: "
            f"{len(optional)} blocks leave no room for what was removed from them."
        )
    quoted: list[Span] = []
    offset = 0
    for block in blocks:
        if block.quoted:
            quoted.append((offset, offset + len(block.text), block.label))
        offset += len(block.text)
    return PrBody("".join(block.text for block in blocks), quoted)


def comment_parts(body: str, header: str) -> list[str]:
    """Split a full body into comments that each fit GitHub's limit."""
    size = COMMENT_LIMIT - len(header) - 32
    chunks = [body[index:index + size] for index in range(0, len(body), size)]
    return [f"{header} ({index}/{len(chunks)})\n\n{chunk}" for index, chunk in enumerate(chunks, 1)]
