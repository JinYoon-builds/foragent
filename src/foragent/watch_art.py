"""D64 pixel art, independent of terminal drawing and screen layout.

Sprites follow docs/design/lane-view-sketch/cast.py. Pixels are palette keys or
None (transparent); half-block cells carry glyph, foreground and background.
A None colour means an unpainted pixel (the terminal background), including
when only the bottom pixel is painted. Only the upper half-block and ASCII are
used. Consumers must map transparent foregrounds to the terminal background.
"""

from collections.abc import Mapping, Sequence

from foragent.watch_cells import Line


PixelGrid = Sequence[Sequence[str | None]]
Cell = tuple[str, int | None, int | None]

# xterm-256 colours: dark brown outlines remain visible on dark terminals.
PALETTE = {
    "K": 52, "O": 173, "R": 130, "W": 230, "E": 232, "N": 94,
    "A": 137, "C": 58, "P": 209, "T": 173, "Z": 147, "X": 221,
    "D": 250,
}


def half_blocks(grid: PixelGrid, palette: Mapping[str, int]) -> list[list[Cell]]:
    """Pair pixel rows into cells, padding ragged/odd edges with transparency.

    Colour keys must exist in palette. No ANSI escapes or terminal calls are
    needed: consumers can use the returned colours for curses or ANSI drawing.
    """
    width = max((len(row) for row in grid), default=0)
    result = []
    for y in range(0, len(grid), 2):
        top = grid[y]
        bottom = grid[y + 1] if y + 1 < len(grid) else ()
        cells = []
        for x in range(width):
            upper = top[x] if x < len(top) else None
            lower = bottom[x] if x < len(bottom) else None
            foreground = palette[upper] if upper is not None else None
            background = palette[lower] if lower is not None else None
            cells.append((" " if upper is None and lower is None else "▀",
                          foreground, background))
        result.append(cells)
    return result


def _pixels(rows: Sequence[str], width: int) -> tuple[tuple[str | None, ...], ...]:
    """Decode the sketch's dot-transparent notation once at import time."""
    return tuple(tuple(None if pixel == "." else pixel for pixel in row.ljust(width, "."))
                 for row in rows)


# Big orchestrator hugging an acorn: 18 pixels wide, 15 high (8 cell rows).
ORCHESTRATOR = _pixels([
    '......KK...KK.....',
    '.....KOOK.KOWK....',
    '.....KOOOKKOOOK...',
    '..KKKKOOOOOOOOOK..',
    '.KWWWKOOOOOOOOOOK.',
    'KWWWWKOOEOOOOEOOK.',
    'KWWWOKOPWWWNWWWPOK',
    'KOWOOKOOWWWWWWWOOK',
    'KOOOOKKOOWCCCWOOK.',
    'KROOOOKOOCAAACOOK.',
    '.KROOOKOOKAAAKOOK.',
    '.KRROOOKOOKKKOOK..',
    '..KKRRROOOOOOOK...',
    '....KKKKKOK.KOK...',
    '........KK...KK...',
], 18)

# Lane squirrels: 9 x 8 pixels (4 cell rows), in the four decided poses.
LANE_POSES = {
    'gnaw': _pixels([
        '....O.O..',
        'WT..OOOO.',
        'TT.OOEOCC',
        'TT.OOOWAA',
        'TT.OWWOA.',
        '.TOOWWO..',
        '..OOOOO..',
        '..O...O..',
    ], 9),
    'sit': _pixels([
        '....O.O..',
        'WT..OOOO.',
        'TT.OOEOO.',
        'TT.OOOWWN',
        'TT.OWWO..',
        '.TOOWWO..',
        '..OOOOO..',
        '..O...O..',
    ], 9),
    'wave': _pixels([
        '....O.OXO',
        'WT..OOOOO',
        'TT.OOEOO.',
        'TT.OOOWWN',
        'TT.OWWO..',
        '.TOOWWO..',
        '..OOOOO..',
        '..O...O..',
    ], 9),
    'sleep': _pixels([
        '.........',
        '.......Z.',
        '......Z..',
        '.........',
        'WTT.OOOO.',
        'TTTOOKOOW',
        'TTTOOOOOO',
        '.TTOOOOO.',
    ], 9),
}


# A lane squirrel watching for work: 11 x 8 pixels (4 cell rows).
_LOOKOUT = _pixels([
    '....O.O....',
    '...OOOO....',
    '..OOOEO....',
    'T..OOOO....',
    'TT.OWWO....',
    'TT.OWWO....',
    '.TTOOOO....',
    '...O..O....',
], 11)

# A lane squirrel asleep on its side: 9 x 8 pixels (4 cell rows).
_LYING_ASLEEP = _pixels([
    '.........',
    '.........',
    '.......Z.',
    'WT.......',
    'TT..O.O..',
    'TT.OOOOO.',
    'TTOOOKOOO',
    '.TOOOOOO.',
], 9)


def lookout(tick: int) -> PixelGrid:
    """The watching squirrel's frame for one animation tick."""
    return _LOOKOUT


def lying_asleep(tick: int) -> PixelGrid:
    """The sleeping squirrel's frame for one animation frame (callers pass tick // 2)."""
    return _LYING_ASLEEP


def pixel_rows(grid: PixelGrid, palette: Mapping[str, int] = PALETTE) -> list[Line]:
    """half_blocks as styled spans: ("art", fg, bg) per cell, never ANSI.

    A bottom-only cell becomes a lower half-block in its own colour, so its upper half
    is the terminal background rather than the default text colour.
    """
    rows = []
    for cells in half_blocks(grid, palette):
        row: Line = []
        for glyph, upper, lower in cells:
            if glyph == " ":
                row.append((" ", ("art", None, None)))
            elif upper is None:
                row.append(("▄", ("art", lower, None)))
            else:
                row.append(("▀", ("art", upper, lower)))
        rows.append(row)
    return rows


# Sideways acorn colours by canonical style: (stem and cap fg, body fg, body bg).
_ACORN_COLOURS = {"working": (94, 232, 220), "question": (94, 232, 203), "next": (237, 252, 240)}
_ACORN_STYLES = {"yellow": "working", "working": "working", "question": "question"}


def sideways_acorn(number: int, word: str, style: str, *, tick: int = 0, ascii: bool = False) -> Line:
    """One issue as a sideways acorn ``◖█#N word▌``, or ``[#N word]`` in ASCII.

    yellow means working; parked and unknown styles use the next colours, so unknown
    work never looks running.
    """
    style = _ACORN_STYLES.get(style, "next")
    label = f"#{number} {word}" if word else f"#{number}"
    if ascii:
        return [(f"[{label}]", style)]
    cap, ink, body = _ACORN_COLOURS[style]
    return [("◖", ("art", cap, None)), ("█", ("art", cap, None)),
            (label, ("art", ink, body)), ("▌", ("art", body, None))]


def acorn_colour(done: bool) -> int:
    """Completed nuts are brown; remaining nuts are grey (xterm-256)."""
    return PALETTE["A"] if done else 240


def acorn(number: int | None, done: bool) -> list[str]:
    """Three ANSI-coloured rows: cap, issue number on the body, pointed tip.

    ASCII edges avoid ambiguous-width block decorations in Korean terminals.
    The body grows with the number; None draws an unnumbered preparation nut.
    Every row has the same visible width and resets its colours afterwards.
    """
    colour = acorn_colour(done)
    label = f"#{number}" if number is not None else "  "
    width = len(label) + 2
    left = (width - 1) // 2
    reset = "\x1b[0m"
    foreground = f"\x1b[38;5;{colour}m"
    body = f"\x1b[48;5;{colour}m\x1b[38;5;230m{label}{reset}"
    return [
        foreground + "/" + "=" * len(label) + "\\" + reset,
        foreground + "(" + body + foreground + ")" + reset,
        " " * left + foreground + "v" + reset + " " * (width - left - 1),
    ]


def ansi_rows(grid: PixelGrid, palette: Mapping[str, int]) -> list[str]:
    """half_blocks as xterm-256 ANSI text, one string per cell row, glyphs unchanged.

    A None colour sets no SGR code, so that half shows the terminal's own colour.
    Each painted cell resets afterwards; trailing transparent cells are dropped.
    """
    rows = []
    for cells in half_blocks(grid, palette):
        row = ""
        for glyph, foreground, background in cells:
            if glyph == " ":
                row += glyph
                continue
            codes = [f"38;5;{foreground}"] * (foreground is not None) + [f"48;5;{background}"] * (background is not None)
            row += f"\x1b[{';'.join(codes)}m{glyph}\x1b[0m"
        rows.append(row.rstrip(" "))
    return rows
