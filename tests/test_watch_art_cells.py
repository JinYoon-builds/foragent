"""Renderer edge cases and the reusable sprite palette."""

import re

import pytest

from foragent import watch_art


def test_ragged_rows_pad_transparently_and_keep_colour_zero():
    assert watch_art.half_blocks([["ink"], [], [None, "ink"]], {"ink": 0}) == [
        [("▀", 0, None), (" ", None, None)],
        [(" ", None, None), ("▀", 0, None)],
    ]


def test_supplied_palette_covers_every_sprite():
    for grid in [watch_art.ORCHESTRATOR, *watch_art.LANE_POSES.values()]:
        assert watch_art.half_blocks(grid, watch_art.PALETTE)


@pytest.mark.parametrize("number", [None, 0, 7, 151, 9876543210])
def test_acorn_rows_keep_equal_visible_width_and_reset_colours(number):
    for done in (False, True):
        rows = watch_art.acorn(number, done)
        plain = [re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows]
        assert len({len(row) for row in plain}) == 1
        assert all("\x1b[0m" in row for row in rows)
        assert f"\x1b[48;5;{watch_art.acorn_colour(done)}m" in rows[1]
