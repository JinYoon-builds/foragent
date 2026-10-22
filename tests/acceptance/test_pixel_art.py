"""Acceptance test for pixel-art. Locked by Foragent; do not edit."""

import re
import unicodedata


def art_plain(value):
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


def test_pixel_art_half_blocks_preserve_upper_lower_empty_and_odd_pixels():
    from foragent.watch_art import half_blocks

    palette = {"brown": 130, "grey": 240}
    grid = [["brown", None, None, "grey"], ["grey", "brown", None, None],
            [None, "grey", "brown", None]]
    assert half_blocks(grid, palette) == [
        [("▀", 130, 240), ("▀", None, 130), (" ", None, None), ("▀", 240, None)],
        [(" ", None, None), ("▀", 240, None), ("▀", 130, None), (" ", None, None)],
    ]
    assert half_blocks([], palette) == []
    assert half_blocks([[None], [None]], palette) == [[(" ", None, None)]]


def test_pixel_art_sprites_have_the_decided_dimensions_and_four_poses():
    from foragent.watch_art import LANE_POSES, ORCHESTRATOR, half_blocks

    assert ORCHESTRATOR and all(len(row) == 18 for row in ORCHESTRATOR)
    assert set(LANE_POSES) == {"gnaw", "sit", "wave", "sleep"}
    assert len({repr(grid) for grid in LANE_POSES.values()}) == 4
    for grid in [ORCHESTRATOR, *LANE_POSES.values()]:
        palette = {colour: index for index, colour in enumerate(
            dict.fromkeys(pixel for row in grid for pixel in row if pixel is not None))}
        rendered = half_blocks(grid, palette)
        assert len(rendered) == (len(grid) + 1) // 2
        assert any(glyph == "▀" for row in rendered for glyph, _, _ in row)
    for grid in LANE_POSES.values():
        assert len(grid) == 8 and all(len(row) == 9 for row in grid)


def test_pixel_art_acorn_number_is_on_body_and_completion_changes_colour(monkeypatch):
    from foragent import watch_art

    colour = watch_art.acorn_colour
    assert colour(False) != colour(True)
    seen = []

    def record(done):
        seen.append(done)
        return colour(done)

    monkeypatch.setattr(watch_art, "acorn_colour", record)
    for number in (7, 151, 12345):
        pending = watch_art.acorn(number, False)
        finished = watch_art.acorn(number, True)
        assert isinstance(pending, list) and len(pending) == len(finished) == 3
        assert all(isinstance(line, str) for line in pending + finished)
        assert f"#{number}" in art_plain(pending[1])
        assert f"#{number}" in art_plain(finished[1])
        assert all("#" not in art_plain(line) for line in (pending[0], pending[2]))
        assert list(map(art_plain, pending)) == list(map(art_plain, finished))
        assert pending != finished, "the displayed colour must change, not just the colour helper"
    assert False in seen and True in seen, "acorn must use acorn_colour"
    unnumbered = "".join(map(art_plain, watch_art.acorn(None, False)))
    assert "#" not in unnumbered and "None" not in unnumbered
    assert not any(char.isdigit() for char in unnumbered)


def test_pixel_art_every_emitted_art_glyph_has_unambiguous_width():
    from foragent.watch_art import LANE_POSES, ORCHESTRATOR, acorn, half_blocks

    glyphs = set()
    for grid in [ORCHESTRATOR, *LANE_POSES.values()]:
        palette = {pixel: 1 for row in grid for pixel in row if pixel is not None}
        glyphs.update(glyph for row in half_blocks(grid, palette) for glyph, _, _ in row)
    for number in (None, 0, 151, 9876543210):
        for done in (False, True):
            glyphs.update(art_plain("".join(acorn(number, done))))
    assert not glyphs.intersection("◀▶◆")
    # D64 explicitly requires ▀, although Unicode classifies that half-block as A.
    # All other emitted art, including the acorn cap/body/tip, must be N/Na/H.
    unsafe = {char for char in glyphs - {"▀"}
              if unicodedata.east_asian_width(char) not in {"N", "Na", "H"}}
    assert not unsafe, f"ambiguous or wide art glyphs: {sorted(unsafe)}"
