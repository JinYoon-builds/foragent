"""Character budgeting for PR bodies; independent of the tracker and state store."""

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from itertools import groupby
from pathlib import Path
from typing import Self

PR_BODY_LIMIT = 65_536

# A marker owns its line ending so a shortened neighbour cannot strand it mid-line.
_MARKER = re.compile(r"<!-- (?P<close>/?)foragent:(?P<name>[^>]*?)\s*-->[ \t]*\r?\n?")
_PAIRABLE_NAME = re.compile(r"[\w-]+")
_CLOSING_LINE = re.compile(r"Closes #\d+\s*")
_REQUIRED_LINE = re.compile(
    r"(?P<verdict>.*\bVERDICT:.*)|"
    r"(?P<gate>Opened by Foragent gate.*|Sub-issues of #\d+.*|#\d+의 하위 이슈.*)|"
    r"(?P<checkbox>- \[[ xX]\].*)|"
    r"(?P<notice>_Shortened to fit GitHub.*|_GitHub 제한.*)"
)
_CHECKS_TABLE = "| Done when command | Result |"
_SUMMARY_HEADINGS = {"## What changed", "## Summary", "## Review scope"}
# Headings the gate renders; text quoting many others must not push these out.
_GATE_HEADINGS = _SUMMARY_HEADINGS | {
    "## Review", "## Rebuttals", "## Searched", "## Needs a human check", "## Outside lane",
    "## Lane skills", "## Try it", "## Expect", "## Children",
}

# How a piece gives way, in the order fitting reaches for it: expandable text
# first, then a fair share among required blocks, then locked pieces.
_QUOTED, _PROSE, _ROWS, _SHARED, _LOCKED = range(5)
# Locked blocks are `(kind, number)`; a pair's two delimiters share one.
_REFERENCE, _PAIR = range(2)
_FIRST_REFERENCE = (_REFERENCE, 0)


class Quoted(str):
    """Artifact text in a rendered body: shortened freely, never read as structure."""


Spans = tuple[tuple[int, int], ...]


class PrBody(str):
    """A body that knows its own structure, because line patterns cannot tell quotes apart.

    `quoted` spans are artifact text whatever they look like. `owned` spans are
    what the caller just wrote, and they outlast everything else in the body.
    """

    quoted: Spans
    owned: Spans

    def __new__(cls, text: str, *, quoted: Spans = (), owned: Spans = ()) -> Self:
        body = super().__new__(cls, text)
        body.quoted, body.owned = quoted, owned
        return body


def join_pr_body(parts: Iterable[str], separator: str = "") -> PrBody:
    """Join parts, recording `Quoted` parts and the spans of nested bodies."""
    texts: list[str] = []
    quoted: list[tuple[int, int]] = []
    offset = 0
    for index, part in enumerate(parts):
        if index:
            texts.append(separator)
            offset += len(separator)
        if isinstance(part, Quoted) and part:
            quoted.append((offset, offset + len(part)))
        quoted += [(offset + start, offset + end) for start, end in getattr(part, "quoted", ())]
        texts.append(part)
        offset += len(part)
    return PrBody("".join(texts), quoted=tuple(quoted))


def _quoted_record(root: Path, body: str) -> Path:
    digest = sha256(body.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return root / "pr-bodies" / f"quoted-{digest}.json"


def record_quoted(root: Path, body: str) -> None:
    """Keep the spans of a sent body: GitHub returns only its text, and re-review edits that."""
    if quoted := getattr(body, "quoted", ()):
        path = _quoted_record(root, body)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(quoted), encoding="utf-8")


def load_quoted(root: Path, body: str) -> PrBody:
    """The body with its recorded spans; an edited or older PR body has no record."""
    try:
        quoted = json.loads(_quoted_record(root, body).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        quoted = ()
    return PrBody(body, quoted=tuple((start, end) for start, end in quoted))


def change_outside_quoted(body: PrBody, change: Callable[[str], str]) -> PrBody:
    """Apply a text edit that cannot see or match inside the quoted spans."""
    masked: list[str] = []
    offset = 0
    for index, (start, end) in enumerate(body.quoted):
        masked += [body[offset:start], f"\0{index}\0"]
        offset = end
    masked.append(body[offset:])
    parts = re.split(r"\0(\d+)\0", change("".join(masked)))
    return join_pr_body(
        Quoted(body[slice(*body.quoted[int(part)])]) if index % 2 else part
        for index, part in enumerate(parts)
    )


def own_written_text(body: PrBody, closing: str, written: Iterable[str]) -> PrBody:
    """Mark the closing line and each last copy of `written` as the caller's own."""
    owned = []
    if match := re.search(rf"^{re.escape(closing)}[ \t\r]*$", body, re.MULTILINE):
        owned.append((match.start(), match.end()))
    for text in written:
        if text and (start := body.rfind(text)) >= 0:
            owned.append((start, start + len(text)))
    return PrBody(body, quoted=body.quoted, owned=tuple(owned))


@dataclass
class _Piece:
    rank: int
    text: str
    block: tuple = ()
    owned: bool = False


def _classify_text(text: str, section: dict) -> list[_Piece]:
    """Split text outside marker pairs into line pieces; `section` carries state across markers."""
    pieces: list[_Piece] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        bare = line.rstrip("\r\n")
        if stripped.startswith("## "):
            section["name"] = stripped
            section["summary"] = stripped in _SUMMARY_HEADINGS
            section["started"] = False
        elif section["summary"] and stripped:
            section["started"] = True
        elif section["summary"] and section["started"]:
            section["summary"] = False
        name = section["name"]
        if bare.startswith("|"):
            # The heading and rule stay while rows go, so a shortened table still reads as one.
            section["table"] += 1
            section["checks"] = section["checks"] or bare == _CHECKS_TABLE
            if section["checks"]:
                pieces.append(_Piece(_SHARED, line, ("checks", name)))
            elif section["table"] <= 2:
                pieces.append(_Piece(_SHARED, line, ("table", name)))
            else:
                pieces.append(_Piece(_ROWS, line))
            continue
        section["table"], section["checks"] = 0, False
        if _CLOSING_LINE.fullmatch(bare):
            # A quoted repeat of a reference does not need the first one's guarantee.
            if bare.strip() in section["closes"]:
                pieces.append(_Piece(_SHARED, line, ("repeated-reference",)))
            else:
                pieces.append(_Piece(_LOCKED, line, (_REFERENCE, len(section["closes"]))))
                section["closes"].add(bare.strip())
        elif stripped.startswith("## "):
            first = stripped in _GATE_HEADINGS and stripped not in section["headings"]
            section["headings"].add(stripped)
            pieces.append(_Piece(_SHARED, line, ("gate-heading",) if first else ("heading",)))
        elif section["summary"]:
            pieces.append(_Piece(_SHARED, line, ("summary", name)))
        elif match := _REQUIRED_LINE.fullmatch(bare):
            pieces.append(_Piece(_SHARED, line, (match.lastgroup, name)))
        else:
            pieces.append(_Piece(_PROSE, line))
    return pieces


def _pair_markers(markers: list[re.Match]) -> dict[int, int]:
    """Map both delimiters of each pair to its opener's offset, nearest opener first.

    Pairing is by name alone, so a differently named pair nested or crossing inside
    another stays a pair of its own.
    """
    pairs: dict[int, int] = {}
    open_by_name: dict[str, list[int]] = {}
    for match in markers:
        if not _PAIRABLE_NAME.fullmatch(match["name"]):
            continue
        if not match["close"]:
            open_by_name.setdefault(match["name"], []).append(match.start())
        elif open_by_name.get(match["name"]):
            pairs[match.start()] = pairs[opener] = (opener := open_by_name[match["name"]].pop())
    return pairs


def _classify(body: str) -> list[_Piece]:
    """Return pieces whose texts join back into `body`."""
    section = {"name": "", "summary": False, "started": False, "table": 0, "checks": False,
               "closes": set(), "headings": set()}
    pieces: list[_Piece] = []
    quoted = getattr(body, "quoted", ())
    markers = [match for match in _MARKER.finditer(body)
               if not any(start <= match.start() < end for start, end in quoted)]
    pairs = _pair_markers(markers)
    inside: list[int] = []  # Openers of the pairs around the current offset.

    def add_text(text: str) -> None:
        if inside:
            pieces.extend(_Piece(_SHARED, line, ("pair", inside[-1]))
                          for line in text.splitlines(keepends=True))
        else:
            pieces.extend(_classify_text(text, section))

    offset = 0
    events = sorted([(match.start(), match) for match in markers] + [(start, None) for start, _ in quoted],
                    key=lambda event: event[0])
    spans = iter(quoted)
    for start, match in events:
        add_text(body[offset:start])
        if match is None:
            offset = next(spans)[1]
            pieces.append(_Piece(_QUOTED, body[start:offset]))
            continue
        offset = match.end()
        pair = pairs.get(start)
        if pair is None:
            pieces.append(_Piece(_SHARED, match[0], ("lone-marker",)))
            continue
        pieces.append(_Piece(_LOCKED, match[0], (_PAIR, pair)))
        if pair == start:
            inside.append(pair)
        else:
            inside.remove(pair)
    add_text(body[offset:])
    _mark_owned(pieces, getattr(body, "owned", ()))
    return pieces


def _mark_owned(pieces: list[_Piece], owned: Spans) -> None:
    if not owned:
        return
    offset = 0
    for piece in pieces:
        span = next((span for span in owned if span[0] <= offset < span[1]), None)
        if span is not None and piece.rank != _QUOTED:
            piece.owned = True
            if piece.rank != _LOCKED:
                piece.rank, piece.block = _SHARED, ("owned", span[0])
        offset += len(piece.text)


def _cut_prose(text: str, excess: int) -> str:
    kept = text[:max(0, len(text) - excess)]
    # Never join a partial prose line to a following required line.
    return kept[:-1] + "\n" if kept and not kept.endswith("\n") else kept


def _cut_rows(text: str, excess: int) -> str:
    """Drop whole rows from the end; half a row breaks the table it belongs to."""
    rows = text.splitlines(keepends=True)
    while rows and excess > 0:
        excess -= len(rows.pop())
    return "".join(rows)


def _fair_share(sizes: list[int], budget: int) -> int:
    """The largest size cap whose capped total fits the budget, so small blocks stay whole."""
    remaining = max(0, budget)
    ordered = sorted(sizes)
    for index, size in enumerate(ordered):
        share = remaining // (len(ordered) - index)
        if size > share:
            return share
        remaining -= size
    return ordered[-1] if ordered else 0


def _keep_start(block: list[_Piece], cap: int) -> None:
    """Keep whole lines from the start of a block; a first line over the cap keeps its start."""
    used = 0
    for index, piece in enumerate(block):
        if used + len(piece.text) > cap:
            piece.text = _cut_prose(piece.text, len(piece.text) - cap) if index == 0 else ""
            cap = used  # Everything after the first line that missed goes too.
        else:
            used += len(piece.text)


def _share(pieces: list[_Piece], owned: bool, excess: int) -> int:
    """Shrink the largest of these shared blocks until they fit what is left of the budget."""
    blocks: dict[tuple, list[_Piece]] = {}
    for piece in pieces:
        if piece.rank == _SHARED and piece.owned == owned:
            blocks.setdefault(piece.block, []).append(piece)
    sizes = [sum(len(piece.text) for piece in block) for block in blocks.values()]
    cap = _fair_share(sizes, sum(sizes) - excess)
    for block in blocks.values():
        _keep_start(block, cap)
    return excess - sum(sizes) + sum(len(piece.text) for block in blocks.values() for piece in block)


def _drop(pieces: list[_Piece], kind: int, owned: bool, excess: int) -> int:
    """Empty one kind's locked blocks from the end of the body; delimiters go together."""
    blocks: dict[tuple, list[_Piece]] = {}
    for piece in pieces:
        if piece.rank == _LOCKED and piece.block[0] == kind and piece.owned == owned and piece.block != _FIRST_REFERENCE:
            blocks.setdefault(piece.block, []).append(piece)
    for key in sorted(blocks, key=lambda key: key[1], reverse=True):
        if excess <= 0:
            break
        for piece in blocks[key]:
            excess -= len(piece.text)
            piece.text = ""
    return excess


def fit_pr_body(
    body: str, limit: int = PR_BODY_LIMIT, *,
    notice: str = "_Shortened to fit GitHub's limit; full text retained in the state store._",
) -> PrBody:
    """Keep required content whole and shorten the largest expandable runs first.

    When expandable text is gone, required blocks (one per kind in each section, and
    the text of each marker pair) share what is left: the largest give way to a fair
    share and the small stay whole. A pair's delimiters go only together, after every
    other choice, and the first closing reference stays.

    Quoted spans are expandable whatever they look like, and owned spans give way
    after everything else; without either, line patterns are the only evidence.

    Callers retain the original before sending a shortened result and supply a
    notice with its durable location. Length is Python characters, not UTF-8 bytes.
    Bodies that fit are returned verbatim, making repeated fitting a no-op.
    """
    if len(body) <= limit:
        return body if isinstance(body, PrBody) else PrBody(body)

    # One notice covers all shortened runs, and previous notices remain reachable
    # when a later review appends enough material to require another fitting.
    suffix = "\n\n" + notice + "\n"
    excess = len(body) + len(suffix) - limit

    pieces = _classify(body)
    # Neighbouring expandable pieces shorten as one run, largest run first.
    runs = [(rank, list(group)) for rank, group in groupby(pieces, key=lambda piece: piece.rank)]
    expandable = [(rank, group, "".join(piece.text for piece in group))
                  for rank, group in runs if rank in (_QUOTED, _PROSE, _ROWS)]
    for rank, group, content in sorted(expandable, key=lambda run: len(run[2]), reverse=True):
        if excess <= 0:
            break
        kept = (_cut_rows if rank == _ROWS else _cut_prose)(content, excess)
        excess -= len(content) - len(kept)
        group[0].text = kept
        for piece in group[1:]:
            piece.text = ""

    if excess > 0:
        excess = _share(pieces, False, excess)

    # Only more distinct references and pairs than the limit can hold reach here.
    excess = _drop(pieces, _REFERENCE, False, excess)
    excess = _drop(pieces, _PAIR, False, excess)
    if excess > 0:
        excess = _share(pieces, True, excess)
    excess = _drop(pieces, _PAIR, True, excess)

    texts = []
    quoted = []
    offset = 0
    for piece in pieces:
        if piece.rank == _QUOTED and piece.text:
            quoted.append((offset, offset + len(piece.text)))
        texts.append(piece.text)
        offset += len(piece.text)
    # Past this point a single reference and the notice outgrow the limit, so nothing can stay whole.
    return PrBody(("".join(texts) + suffix)[:limit], quoted=tuple(quoted))
