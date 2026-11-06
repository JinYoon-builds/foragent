"""Acceptance: the pixel word beside the greeting squirrel reads FORAGENT.

UX researcher r833: at 120 columns the word read 'EORAGENI'. Letter 1 had a
full bottom bar like E, letter 8 had a bottom bar like a serif I, and in every
letter the upper colour band sat one column right of the lower bands, so each
letter looked like two offset pieces. Decided fix (orchestrator): F has no
bottom bar, T is a top bar with a centred stem only, E keeps its bottom bar,
and within each letter the colour bands line up column for column: the upper band spans the
letter's full width and the strokes below it start and end in the same columns.

Observed only through the installed statusline during a session's first
minute (`fa statusline` after the SessionStart hook) at COLUMNS=120. Half-block
cells are decoded back to pixels. The shadow is the word's darkest colour, the
upper band its lightest; no palette names or glyph data are read.
"""

import pytest

from test_fa_report_since import report_case  # noqa: F401
from test_presence_session_start import session_case  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_squirrel_wordmark import _greeting, _luminance, _pixels, _split

WIDE = 120


def _word_letters(case, monkeypatch):
    """Per letter: the set of (y, x) lit pixels and the set of upper-band pixels."""
    grid = _pixels(_greeting(case, monkeypatch, WIDE))
    _, end = _split(grid)
    word = [(y, x, grid[y][x]) for y in range(len(grid))
            for x in range(end, len(grid[0])) if grid[y][x] is not None]
    assert word, f"At {WIDE} columns the word stands to the right of the squirrel"
    colours = {colour for _, _, colour in word}
    shadow = min(colours, key=_luminance)
    upper = max(colours, key=_luminance)
    lit = [(y, x, colour) for y, x, colour in word if colour != shadow]
    columns = sorted({x for _, x, _ in lit})
    groups = []
    for x in columns:
        if groups and groups[-1][-1] == x - 1:
            groups[-1].append(x)
        else:
            groups.append([x])
    assert len(groups) == len("FORAGENT"), "The word is eight separate pixel letters"
    letters = []
    for group in groups:
        pixels = {(y, x) for y, x, _ in lit if x in group}
        cream = {(y, x) for y, x, colour in lit if x in group and colour == upper}
        letters.append((pixels, cream))
    baseline = max(y for y, _, _ in lit)
    return dict(zip("FORAGENT", letters)) | {"_order": letters}, baseline


def _row(pixels, y):
    return sorted(x for py, x in pixels if py == y)


def _edges(pixels):
    xs = [x for _, x in pixels]
    return min(xs), max(xs)


def test_first_letter_is_f_without_a_bottom_bar_while_e_keeps_its_bar(session_case, monkeypatch):
    letters, baseline = _word_letters(session_case, monkeypatch)
    f_pixels, _ = letters["F"]
    left, right = _edges(f_pixels)
    f_bottom = _row(f_pixels, baseline)
    assert f_bottom, "F's stem reaches the word's bottom row"
    assert max(f_bottom) < (left + right) / 2, (
        f"Letter 1 must read F: its bottom row is only the stem at the left, found columns "
        f"{[x - left for x in f_bottom]} of 0..{right - left} (a full bottom bar reads E)")
    top = min(y for y, _ in f_pixels)
    assert _row(f_pixels, top)[-1] == right, "F keeps its top bar reaching the right edge"

    e_pixels, _ = letters["E"]
    e_left, e_right = _edges(e_pixels)
    assert _row(e_pixels, baseline) == list(range(e_left, e_right + 1)), (
        "E keeps a full bottom bar from its left edge to its right edge")


def test_last_letter_is_t_a_top_bar_and_a_centred_stem_only(session_case, monkeypatch):
    letters, baseline = _word_letters(session_case, monkeypatch)
    t_pixels, _ = letters["T"]
    left, right = _edges(t_pixels)
    top = min(y for y, _ in t_pixels)
    stem = _row(t_pixels, baseline)
    assert stem and left < min(stem) and max(stem) < right, (
        f"Letter 8 must read T: its bottom row is only the stem, found columns "
        f"{[x - left for x in stem]} of 0..{right - left} (a bottom bar reads a serif I)")
    assert _row(t_pixels, top) == list(range(left, right + 1)), "T's top row is a full bar"
    bar = [y for y in range(top, baseline + 1) if _row(t_pixels, y) == list(range(left, right + 1))]
    assert bar == list(range(top, top + len(bar))), "T's only full-width rows are its top bar"
    assert abs((min(stem) - left) - (right - max(stem))) <= 1, "T's stem is centred under the bar"
    for y in range(top + len(bar), baseline + 1):
        assert _row(t_pixels, y) == stem, (
            f"Below the bar every T row is the same centred stem (pixel row {y - top} differs)")


# Letters whose lower strokes reach the letter's left edge, and its right edge.
# T's lower part is a centred stem; F's lower right is a short arm, not an edge.
LEFT_STROKE = set("FORAGEN")
RIGHT_STROKE = set("ORAGEN")


@pytest.mark.parametrize("index", range(len("FORAGENT")), ids=list("FORAGENT"))
def test_colour_bands_of_each_letter_line_up_column_for_column(session_case, monkeypatch, index):
    letters, _ = _word_letters(session_case, monkeypatch)
    name = "FORAGENT"[index]
    pixels, cream = letters["_order"][index]
    lower = pixels - cream
    assert cream and lower, "Each letter shows its upper colour band and the bands below it"
    left, right = _edges(pixels)
    assert _edges(cream) == (left, right), (
        f"Letter {name}: the upper band spans columns {_edges(cream)} but the letter spans "
        f"{(left, right)}; the upper part must not be narrower or shifted against the lower part")
    low_left, low_right = _edges(lower)
    if name in LEFT_STROKE:
        assert low_left == left, f"Letter {name}: the left stroke runs straight down across the colour split"
    if name in RIGHT_STROKE:
        assert low_right == right, f"Letter {name}: the right stroke runs straight down across the colour split"
