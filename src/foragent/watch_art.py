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


# A lane squirrel watching for work: 11 x 8 pixels (4 cell rows). Ticks 0-1 face
# right and 2-3 left; only head rows 0 and 2 turn, and D dots count tick % 4.
_LOOKOUT_BODY = [
    '...OOOO....',
    'T..OOOO....',
    'TT.OWWO....',
    'TT.OWWO....',
    '.TTOOOO....',
    '...O..O....',
]
_LOOKOUT_HEAD = {"right": ('....O.O....', '..OOOEO....'), "left": ('...O.O.....', '..OOEOO....')}
_LOOKOUT_DOTS = ((1, 8), (1, 9), (1, 10))


def _frame(rows: Sequence[str], width: int, key: str,
           points: Sequence[tuple[int, int]]) -> tuple[tuple[str | None, ...], ...]:
    """Paint one palette key at zero-based (y, x) points over dot-notation rows."""
    grid = [list(row) for row in rows]
    for y, x in points:
        grid[y][x] = key
    return _pixels(["".join(row) for row in grid], width)


def _lookout_frame(tick: int) -> tuple[tuple[str | None, ...], ...]:
    ear, eye = _LOOKOUT_HEAD["right" if tick < 2 else "left"]
    return _frame([ear, _LOOKOUT_BODY[0], eye, *_LOOKOUT_BODY[1:]], 11, "D", _LOOKOUT_DOTS[:tick])


_LOOKOUT_FRAMES = tuple(_lookout_frame(tick) for tick in range(4))

# A lane squirrel asleep on its side: 9 x 8 pixels (4 cell rows); Z drifts up.
_LYING_ROWS = [
    '.........',
    '.........',
    '.........',
    'WT.......',
    'TT..O.O..',
    'TT.OOOOO.',
    'TTOOOKOOO',
    '.TOOOOOO.',
]
_LYING_ZS = (((2, 7),), ((2, 7), (1, 8)), ((1, 8), (0, 8)), ((0, 8),))
_LYING_FRAMES = tuple(_frame(_LYING_ROWS, 9, "Z", points) for points in _LYING_ZS)


def lookout(tick: int) -> PixelGrid:
    """The watching squirrel's frame for one animation tick."""
    return _LOOKOUT_FRAMES[tick % 4]


def lying_asleep(tick: int) -> PixelGrid:
    """The sleeping squirrel's frame for one animation frame (callers pass tick // 2)."""
    return _LYING_FRAMES[tick % 4]


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
_WORKING_STEMS = ("◖", "◜", "◖", "◟")


def sideways_acorn(number: int, word: str, style: str, *, tick: int = 0, ascii: bool = False) -> Line:
    """One issue as a sideways acorn ``◖█#N word▌``, or ``[#N word]`` in ASCII.

    yellow means working; parked and unknown styles use the next colours, so unknown
    work never looks running. Only working stems rock with tick; ASCII ignores tick.
    """
    style = _ACORN_STYLES.get(style, "next")
    label = f"#{number} {word}" if word else f"#{number}"
    if ascii:
        return [(f"[{label}]", style)]
    cap, ink, body = _ACORN_COLOURS[style]
    stem = _WORKING_STEMS[tick % 4] if style == "working" else "◖"
    return [(stem, ("art", cap, None)), ("█", ("art", cap, None)),
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
    """half_blocks as xterm-256 ANSI text, one string per cell row.

    Only painted halves get colour; the other half stays the terminal's own colour.
    A bottom-only cell is a lower half-block in foreground ink, never an upper block
    drawn in the default text colour. Each painted cell resets afterwards, and
    trailing transparent cells are dropped.
    """
    rows = []
    for cells in half_blocks(grid, palette):
        row = ""
        for glyph, upper, lower in cells:
            if glyph == " ":
                row += glyph
                continue
            if upper is None:
                glyph, codes = "▄", [f"38;5;{lower}"]
            else:
                codes = [f"38;5;{upper}"] + [f"48;5;{lower}"] * (lower is not None)
            row += f"\x1b[{';'.join(codes)}m{glyph}\x1b[0m"
        rows.append(row.rstrip(" "))
    return rows
