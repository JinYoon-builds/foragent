"""Terminal-cell text and styled-line composition.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
"""

from typing import Any


Span = tuple[str, str]
Line = list[Span]


def _clean(value: Any) -> str:
    from foragent import watch

    return " ".join("".join(c if c in "\n\r\t" else "" if watch.unicodedata.category(c).startswith("C") else c
                            for c in str(value)).split())


def _cells(value: str) -> int:
    from foragent import watch

    return sum(0 if watch.unicodedata.combining(c) or watch.unicodedata.category(c) in {"Mn", "Me", "Cf"}
               else 2 if watch.unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in value)


def _fit(line: Line, width: int) -> Line:
    """Clip and pad in terminal cells, preserving the color spans."""
    from foragent import watch

    result: Line = []
    remaining = max(0, width)
    for value, style in line:
        clipped = ""
        for char in value:
            size = watch._cells(char)
            if size > remaining:
                break
            clipped += char
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

    lines: list[Line] = []
    current = ""
    for word in watch._clean(value).split():
        if current and watch._cells(current + " " + word) > width:
            lines.append([(current, style)])
            current = ""
        while watch._cells(word) > width:
            piece = watch._fit([(word, style)], width)[0][0]
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
