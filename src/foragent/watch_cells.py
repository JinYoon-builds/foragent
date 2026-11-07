"""Terminal-cell text and styled-line composition.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
"""

from typing import Any


# A named style ("working", "dim") or a structured pixel colour ("art", fg, bg) with
# xterm-256 indices; None is the terminal default. Lines never carry ANSI escapes.
Style = str | tuple[str, int | None, int | None]
Span = tuple[str, Style]
Line = list[Span]


def _clean(value: Any) -> str:
    from foragent import watch

    return " ".join("".join(c if c in "\n\r\t" else "" if watch.unicodedata.category(c).startswith("C") else c
                            for c in str(value)).split())


# Emoji the lane rows use whose Unicode width is narrow by default; terminals draw them two cells wide.
WIDE_EMOJI = frozenset("🐿")


# VARIATION SELECTOR-16 asks for emoji presentation: terminals draw the character before it
# (for example U+1F6E0 or U+2699) two cells wide even when its Unicode width is narrow.
VS16 = "\ufe0f"


def _char_cells(char: str) -> int:
    from foragent import watch

    if watch.unicodedata.combining(char) or watch.unicodedata.category(char) in {"Mn", "Me", "Cf"}:
        return 0
    return 2 if char in WIDE_EMOJI or watch.unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _clusters(value: str) -> list[str]:
    """Characters with the zero-width marks that follow them, so a cut never splits a pair."""
    clusters: list[str] = []
    for char in value:
        if clusters and _char_cells(char) == 0:
            clusters[-1] += char
        else:
            clusters.append(char)
    return clusters


def _cells(value: str) -> int:
    total = 0
    for cluster in _clusters(value):
        size = _char_cells(cluster[0])
        total += 2 if size == 1 and VS16 in cluster[1:] else size
    return total


def _fit(line: Line, width: int) -> Line:
    """Clip and pad in terminal cells, preserving the color spans."""
    from foragent import watch

    result: Line = []
    remaining = max(0, width)
    for value, style in line:
        clipped = ""
        for cluster in _clusters(value):
            size = watch._cells(cluster)
            if size > remaining:
                break
            clipped += cluster
            remaining -= size
        result.append((clipped, style))
        if remaining == 0 or len(clipped) < len(value):
            break
    if remaining:
        result.append((" " * remaining, "road"))
    return result


def _line(value: Any, style: str = "") -> Line:
    from foragent import watch

    return [(watch._clean(value), style)]


def _wrap(value: str, width: int, style: str = "") -> list[Line]:
    from foragent import watch

    # Without the old 40-column clamp a caller may pass any width; every line takes at
    # least one character, so wrapping always ends (the frame cuts what still overflows).
    width = max(1, width)
    lines: list[Line] = []
    current = ""
    for word in watch._clean(value).split():
        if current and watch._cells(current + " " + word) > width:
            lines.append([(current, style)])
            current = ""
        while watch._cells(word) > width:
            piece = watch._fit([(word, style)], width)[0][0] or word[0]
            lines.append([(piece, style)])
            word = word[len(piece):]
        current += (" " if current else "") + word
    return lines + [[(current, style)]] if current or not lines else lines


def _columns(values: list[str], widths: list[int], style: str = "") -> list[Line]:
    """Keep each table record on one row, marking clipped cells explicitly."""
    from foragent import watch

    row: Line = []
    for index, (value, width) in enumerate(zip(values, widths)):
        if index:
            row.append((" ", ""))
        value = watch._clean(value)
        if watch._cells(value) > width:
            value = "".join(part for part, _ in watch._fit(watch._line(value, style), width - 1)).rstrip() + "…"
        row.extend(watch._fit(watch._line(value, style), width))
    return [row]


def _panel(lines: list[Line], width: int, title: str) -> list[Line]:
    from foragent import watch

    inside = width - 2
    heading = watch._fit(watch._line(f" {title} ", "bold"), inside)
    return [[("┌", "edge"), *heading, ("┐", "edge")],
            *[[("│", "edge"), *watch._fit(line, inside), ("│", "edge")] for line in lines],
            [("└" + "─" * inside + "┘", "edge")]]


def _beside(left: list[Line], right: list[Line], left_width: int, *, links: bool = False) -> list[Line]:
    from foragent import watch

    return [(*watch._fit(a, left_width), ("▶ " if links and index % 3 == 1 else "  ", "blue"), *b)
            for index, (a, b) in enumerate(watch.zip_longest(left, right, fillvalue=[]))]
