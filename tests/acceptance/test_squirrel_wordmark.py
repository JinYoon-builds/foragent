"""Acceptance: the big squirrel has symmetric ears and the FORAGENT word beside it.

Jin 2026-11-04 (PM r525, r526). The big squirrel greets a new session in the
statusline and sits in the dashboard header. Its two ears mirror each other
around the face centre (the midpoint of the eyes), both with the light inner
pixel, and the outline stays closed where the ears meet the head. In the
greeting, the word FORAGENT in pixel letters stands to the right of the
squirrel: the same half-block cells, the same rows, the squirrel's own colours.
When the terminal is too narrow for the word, only the squirrel shows. Every
place that shows the big squirrel draws the same art.

Observed only through the installed statusline (`fa statusline` during a
session's first minute) and the dashboard frame (`watch.render_frame`). The
terminal width reaches the statusline through COLUMNS.
"""

import re

import pytest

from test_fa_report_since import report_case  # noqa: F401
from test_presence_session_start import session_case  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_squirrel_running import plain_frame, single_worker, utf8_terminal  # noqa: F401

SGR = re.compile(r"\x1b\[([0-9;]*)m")
ART = {"▀", "▄", "█"}
LETTERS = len("FORAGENT")
WIDE, NARROW = 120, 60


def _rgb(index):
    if index < 16:
        return ("basic", index)
    if index >= 232:
        return (8 + 10 * (index - 232),) * 3
    levels = (0, 95, 135, 175, 215, 255)
    value = index - 16
    return levels[value // 36], levels[value // 6 % 6], levels[value % 6]


def _cells(line):
    """(glyph, foreground, background) per character, decoding 256-colour and truecolour SGR."""
    cells, fg, bg, reverse, offset = [], None, None, False, 0

    def take(text):
        colours = (bg, fg) if reverse else (fg, bg)
        cells.extend((glyph, *colours) for glyph in text)

    for match in SGR.finditer(line):
        take(line[offset:match.start()])
        codes = [int(code or 0) for code in match.group(1).split(";")]
        at = 0
        while at < len(codes):
            code = codes[at]
            if code == 0:
                fg, bg, reverse = None, None, False
            elif code in (7, 27):
                reverse = code == 7
            elif code == 39:
                fg = None
            elif code == 49:
                bg = None
            elif code in (38, 48):
                mode = codes[at + 1]
                colour = _rgb(codes[at + 2]) if mode == 5 else tuple(codes[at + 2:at + 5])
                fg, bg = (colour, bg) if code == 38 else (fg, colour)
                at += 2 if mode == 5 else 4
            at += 1
        offset = match.end()
    take(line[offset:])
    return cells


def _greeting(case, monkeypatch, columns):
    monkeypatch.setenv("COLUMNS", str(columns))
    case.hook()
    output = case.status()
    lines = output.splitlines()
    rows = [index for index, line in enumerate(lines) if ART & set(line)]
    assert rows, "During the first minute the statusline shows the big squirrel"
    assert rows == list(range(rows[0], rows[0] + len(rows))), "The art is one block of rows"
    for index in rows:
        assert len(_cells(lines[index])) <= columns, f"No art row may be wider than {columns} columns"
    return [lines[index] for index in rows]


def _pixels(art_lines):
    """Decode half-block cells into a pixel grid of colours (None is transparent)."""
    decoded = [_cells(line) for line in art_lines]
    width = max(len(row) for row in decoded)
    grid = [[None] * width for _ in range(2 * len(decoded))]
    for y, row in enumerate(decoded):
        for x, (glyph, fg, bg) in enumerate(row):
            if glyph == " ":
                upper = lower = bg
            elif glyph == "▀":
                upper, lower = fg, bg
            elif glyph == "▄":
                upper, lower = bg, fg
            elif glyph == "█":
                upper = lower = fg
            else:
                pytest.fail(f"Art rows hold only half-block pixel cells, found {glyph!r}")
            grid[2 * y][x], grid[2 * y + 1][x] = upper, lower
    return grid


def _split(grid):
    """The squirrel is the first column block with no empty column; the rest is the word."""
    painted = [any(row[x] is not None for row in grid) for x in range(len(grid[0]))]
    start = painted.index(True)
    end = painted.index(False, start) if False in painted[start:] else len(painted)
    return start, end


def _luminance(colour):
    return sum(weight * value for weight, value in zip((0.299, 0.587, 0.114), colour))


def _painted(grid, columns):
    return [(y, x, grid[y][x]) for y in range(len(grid)) for x in columns if grid[y][x] is not None]


def _runs(row, columns):
    runs, inside = 0, False
    for x in columns:
        if row[x] is not None and not inside:
            runs += 1
        inside = row[x] is not None
    return runs


def _assert_ears(grid, start, end):
    columns = range(start, end)
    pixels = _painted(grid, columns)
    colours = {colour for _, _, colour in pixels}

    # The eyes: the darkest colour that occurs exactly twice, both on one row.
    pairs = []
    for colour in colours:
        spots = [(y, x) for y, x, c in pixels if c == colour]
        if len(spots) == 2 and spots[0][0] == spots[1][0]:
            pairs.append((_luminance(colour), spots))
    assert pairs, "The squirrel keeps two eyes of one colour on one row"
    _, ((_, left_eye), (_, right_eye)) = min(pairs)
    axis2 = left_eye + right_eye  # mirror(x) = axis2 - x, around the eyes' midpoint

    top = min(y for y, _, _ in pixels)
    merge = next(y for y in range(top, len(grid)) if _runs(grid[y], columns) == 1)
    assert merge > top and all(_runs(grid[y], columns) == 2 for y in range(top, merge)), (
        "The art starts with two separate ears that then join the head")
    span = [x for y, x, _ in pixels if top <= y < merge]
    left, right = min(span), max(span)
    assert left + right == axis2, "The ears' outer edges mirror around the face centre (the eyes' midpoint)"

    for y in range(top, merge + 1):
        xs = range(start, end) if y < merge else range(left, right + 1)
        for x in xs:
            mirror = axis2 - x
            other = grid[y][mirror] if start <= mirror < end else None
            assert grid[y][x] == other, (
                f"Ear pixel row {y - top}, column {x}: the left and right ears mirror each other "
                "around the face centre, colours included")

    lightest = max(colours, key=_luminance)
    ears = [(x, colour) for y, x, colour in pixels if top <= y <= merge]
    assert any(c == lightest and 2 * x < axis2 for x, c in ears) and any(
        c == lightest and 2 * x > axis2 for x, c in ears), "Both ears have the light inner pixel"

    outline = {grid[top][x] for x in columns if grid[top][x] is not None}
    assert len(outline) == 1, "The ear tips are drawn in the outline colour"
    for y in range(top, merge + 2):
        for x in columns:
            if grid[y][x] is None or grid[y][x] in outline:
                continue
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                assert 0 <= ny < len(grid) and start <= nx < end and grid[ny][nx] is not None, (
                    f"Pixel row {y - top}: the outline stays closed around the ears and the head")
    return colours


def _palette(key):
    from foragent import watch_art

    return _rgb(watch_art.PALETTE[key])


def _letters(grid, start, lit):
    """Column groups of lit (non-shadow) word pixels, one per letter."""
    groups, current = [], None
    for x in range(start, len(grid[0])):
        if any(row[x] in lit for row in grid):
            current = current or []
            current.append(x)
        elif current:
            groups.append(current)
            current = None
    return groups + ([current] if current else [])


def test_greeting_squirrel_ears_mirror_around_the_face_centre(session_case, monkeypatch):
    grid = _pixels(_greeting(session_case, monkeypatch, WIDE))
    start, end = _split(grid)
    _assert_ears(grid, start, end)


def test_wide_greeting_has_the_foragent_word_right_of_the_squirrel_in_its_rows(session_case, monkeypatch):
    lines = _greeting(session_case, monkeypatch, WIDE)
    grid = _pixels(lines)
    start, end = _split(grid)
    _assert_ears(grid, start, end)
    word = _painted(grid, range(end, len(grid[0])))
    assert word, f"At {WIDE} columns the word FORAGENT stands to the right of the squirrel"
    squirrel_rows = [y for y, _, _ in _painted(grid, range(start, end))]
    word_rows = [y for y, _, _ in word]
    assert min(word_rows) // 2 == min(squirrel_rows) // 2 and max(word_rows) // 2 == max(squirrel_rows) // 2, (
        "The word spans the same cell rows as the squirrel: same scale, same height")
    lit = {_palette("W"), _palette("O"), _palette("R")}
    assert len(_letters(grid, end, lit)) == LETTERS, "The word is drawn once, as eight separate pixel letters"
    assert "FORAGENT" not in plain_frame("\n".join(lines)), "The word is pixel art, not text"


def test_word_logo_has_three_colour_bands_an_offset_shadow_and_trapezoid_letters(session_case, monkeypatch):
    grid = _pixels(_greeting(session_case, monkeypatch, WIDE))
    _, end = _split(grid)
    word = _painted(grid, range(end, len(grid[0])))
    assert word, f"At {WIDE} columns the word FORAGENT stands to the right of the squirrel"
    cream, orange, brown, shadow = (_palette(key) for key in "WORK")
    assert {colour for _, _, colour in word} <= {cream, orange, brown, shadow}, (
        "The word uses only the squirrel's cream, orange, brown and dark outline colours")

    rows = {colour: [y for y, _, c in word if c == colour] for colour in (cream, orange, brown)}
    assert all(rows.values()), "The letters show all three bands: cream, orange and brown"
    assert max(rows[cream]) < min(rows[orange]) and max(rows[orange]) < min(rows[brown]), (
        "Cream rows sit above orange rows, which sit above brown rows")

    lit = {cream, orange, brown}
    letters = [(y, x) for y, x, c in word if c in lit]
    outline = [(y, x) for y, x, c in word if c == shadow]
    assert outline, "A dark shadow outline sits behind the letters"
    assert max(x for _, x in outline) > max(x for _, x in letters) or max(y for y, _ in outline) > max(
        y for y, _ in letters), "The shadow is offset from the letters, not only around them"

    groups = _letters(grid, end, lit)
    assert len(groups) == LETTERS
    for index, columns in enumerate(groups):
        spots = [(y, x) for y, x in letters if x in columns]
        top, bottom = min(y for y, _ in spots), max(y for y, _ in spots)

        def span(row):
            xs = [x for y, x in spots if y == row]
            return max(xs) - min(xs) + 1

        assert span(bottom) >= span(top), (
            f"Letter {'FORAGENT'[index]} is a trapezoid: at least as wide at its bottom row as at its top row")


def test_narrow_greeting_keeps_the_whole_squirrel_and_drops_the_word(session_case, monkeypatch):
    wide = _pixels(_greeting(session_case, monkeypatch, WIDE))
    start, end = _split(wide)
    narrow = _pixels(_greeting(session_case, monkeypatch, NARROW))
    n_start, n_end = _split(narrow)
    assert not _painted(narrow, range(n_end, len(narrow[0]))), (
        f"At {NARROW} columns the word does not fit, so no part of it is drawn")
    assert [row[n_start:n_end] for row in narrow] == [row[start:end] for row in wide], (
        "The squirrel is drawn whole and unchanged on a narrow terminal")


def test_dashboard_header_draws_the_same_squirrel_as_the_greeting(session_case, monkeypatch, tmp_path,
                                                                 utf8_terminal):
    from foragent.watch import render_frame

    greeting = _greeting(session_case, monkeypatch, WIDE)
    start, end = _split(_pixels(greeting))
    expected = ["".join(glyph for glyph, _, _ in _cells(line)[:end]).rstrip() for line in greeting]

    snapshot, config = single_worker()
    lines = plain_frame(render_frame(snapshot, config, tmp_path, width=WIDE)).splitlines()
    first = next(index for index, line in enumerate(lines) if ART & set(line))
    header = [line[:end].rstrip() for line in lines[first:first + len(expected)]]
    assert header == expected, "The dashboard header and the greeting draw the big squirrel from one art"
