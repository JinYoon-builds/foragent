"""Shared squirrel geometry and width-aware greeting composition."""

import re

import pytest

from foragent import watch_art as art


SGR = re.compile(r"\x1b\[[0-9;]*m")


def test_ears_mirror_eyes_and_keep_light_centres_inside_closed_outlines():
    grid = art.ORCHESTRATOR
    assert (len(grid), {len(row) for row in grid}) == (15, {18})
    eyes = [(y, x) for y, row in enumerate(grid) for x, pixel in enumerate(row) if pixel == "E"]
    assert eyes == [(5, 8), (5, 13)]
    axis = eyes[0][1] + eyes[1][1]
    for y in range(3):
        for x in range(6, 16):
            assert grid[y][x] == grid[y][axis - x]
    assert grid[1][8] == grid[1][13] == "W"
    for y in range(4):
        for x, pixel in enumerate(grid[y]):
            if pixel not in (None, "K"):
                assert all(grid[ny][nx] is not None for ny, nx in
                           ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)))


@pytest.mark.parametrize("columns", [None, -1, 0, 1, 18, 60, 83])
def test_unknown_or_too_narrow_greeting_is_exactly_the_whole_squirrel(columns):
    assert art.greeting_rows(columns) == art.ansi_rows(art.ORCHESTRATOR, art.PALETTE)


@pytest.mark.parametrize("columns", [84, 85, 120])
def test_fitting_greeting_keeps_all_squirrel_rows_and_the_gap(columns):
    squirrel = art.ansi_rows(art.ORCHESTRATOR, art.PALETTE)
    rows = art.greeting_rows(columns)
    assert len(rows) == len(squirrel) == 8
    for actual, original in zip(rows, squirrel):
        assert actual.startswith(original)
        plain = SGR.sub("", actual)
        assert plain[18:20] == "  "
        assert 60 < len(plain) <= columns
    assert rows == art.greeting_rows(84)


def test_word_has_eight_tapered_letters_three_bands_and_only_offset_shadow():
    grid = art.WORDMARK
    lit = {"W", "O", "R"}
    groups = []
    for x in range(len(grid[0])):
        if any(row[x] in lit for row in grid):
            if not groups or groups[-1][-1] != x - 1:
                groups.append([])
            groups[-1].append(x)
    assert len(groups) == len("FORAGENT")
    assert len(grid) == len(art.ORCHESTRATOR)
    assert {pixel for row in grid for pixel in row} == {None, "W", "O", "R", "K"}
    for y, row in enumerate(grid):
        for x, pixel in enumerate(row):
            if pixel in lit:
                assert pixel == ("W" if y < 5 else "O" if y < 10 else "R")
            elif pixel == "K":
                assert y > 0 and x > 0 and grid[y - 1][x - 1] in lit
    assert any(pixel == "K" for pixel in grid[-1])
    assert all(pixel not in lit for pixel in grid[-1])
    for columns in groups:
        def span(y):
            xs = [x for x in columns if grid[y][x] in lit]
            return max(xs) - min(xs) + 1
        assert span(13) > span(0)
        assert all(any(grid[y][x] in lit for x in columns) for y in range(14))


@pytest.mark.parametrize("section", ["stage", "squirrels"])
def test_legacy_big_squirrel_paths_preserve_shared_pixels_and_colours(section, tmp_path):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.watch_sections import squirrels, stage

    now = datetime.now(UTC)
    snapshot = SimpleNamespace(
        status={"summary": {}, "history": [], "active_runs": [], "lanes": {}},
        requests=[{"status": "processing", "text": "Hello"}] if section == "stage" else [],
        events=[], say=None, heartbeats={}, captured_at=now,
    )
    module = stage if section == "stage" else squirrels
    rows = module.render(snapshot, Config(), tmp_path, width=44, now=now)
    assert rows[:8] == art.pixel_rows(art.ORCHESTRATOR)
    assert len(rows[0]) == 18  # The dashboard keeps the squirrel alone.
