"""Acceptance test for lane-m2-art. Locked by Foragent; do not edit."""

from test_squirrel_running import plain_frame


def test_bottom_only_pixels_use_lower_foreground_instead_of_default_upper_ink():
    from foragent.watch_art import ansi_rows, half_blocks

    palette = {"top": 130, "bottom": 240}
    grid = [[None, "top", "top", None], ["bottom", None, "bottom", None]]
    # Keep the D64 data contract: only ANSI transport changes the bottom glyph.
    assert half_blocks(grid, palette) == [[
        ("▀", None, 240), ("▀", 130, None), ("▀", 130, 240), (" ", None, None),
    ]]
    row, = ansi_rows(grid, palette)
    assert plain_frame(row) == "▄▀▀", "r52: a bottom-only pixel must be a lower half-block"
    assert "\x1b[38;5;240m▄\x1b[0m" in row, "the lower pixel must use foreground ink"
    assert "\x1b[38;5;130m▀\x1b[0m" in row
    assert "\x1b[38;5;130;48;5;240m▀\x1b[0m" in row


def test_ansi_transparency_resets_between_cells_and_handles_ragged_odd_rows():
    from foragent.watch_art import ansi_rows

    rows = ansi_rows([[None, "a", None], ["b"], [None, "b"]], {"a": 130, "b": 240})
    assert list(map(plain_frame, rows)) == ["▄▀", " ▀"], "r52 must work at ragged and odd edges too"
    assert rows[0].startswith("\x1b[38;5;240m▄\x1b[0m")
    assert "48;5;" not in rows[0], "single painted halves must leave the other half transparent"
    assert ansi_rows([], {}) == []
    assert ansi_rows([[None], [None]], {}) == [""]


def test_ascii_acorn_styles_are_canonical_and_keep_literal_words():
    from foragent.watch_art import sideways_acorn
    for supplied, expected in [('working','working'), ('yellow','working'),
                               ('question','question'), ('next','next'),
                               ('parked','next'), ('unknown','next')]:
        for tick in (0, 1, 2, 3):
            assert sideways_acorn(7, 'literal word', supplied, tick=tick, ascii=True) == [
                ('[#7 literal word]', expected)]
    assert sideways_acorn(7, '', 'parked', ascii=True) == [('[#7]', 'next')]


def test_pixel_frames_and_working_only_stem_animation_match_the_embedded_reference():
    from foragent import watch_art as art
    lookout = [
        ['....O.O....','...OOOO....','..OOOEO....','T..OOOO....','TT.OWWO....','TT.OWWO....','.TTOOOO....','...O..O....'],
        ['....O.O....','...OOOO.D..','..OOOEO....','T..OOOO....','TT.OWWO....','TT.OWWO....','.TTOOOO....','...O..O....'],
        ['...O.O.....','...OOOO.DD.','..OOEOO....','T..OOOO....','TT.OWWO....','TT.OWWO....','.TTOOOO....','...O..O....'],
        ['...O.O.....','...OOOO.DDD','..OOEOO....','T..OOOO....','TT.OWWO....','TT.OWWO....','.TTOOOO....','...O..O....']]
    lying = [
        ['.........','.........','.......Z.','WT.......','TT..O.O..','TT.OOOOO.','TTOOOKOOO','.TOOOOOO.'],
        ['.........','........Z','.......Z.','WT.......','TT..O.O..','TT.OOOOO.','TTOOOKOOO','.TOOOOOO.'],
        ['........Z','........Z','.........','WT.......','TT..O.O..','TT.OOOOO.','TTOOOKOOO','.TOOOOOO.'],
        ['........Z','.........','.........','WT.......','TT..O.O..','TT.OOOOO.','TTOOOKOOO','.TOOOOOO.']]
    for tick in range(8):
        for actual, expected in ((art.lookout(tick),lookout[tick%4]), (art.lying_asleep(tick),lying[tick%4])):
            assert [''.join('.' if cell is None else cell for cell in row) for row in actual] == expected
        for style in ('working','yellow'):
            assert art.sideways_acorn(7,'write',style,tick=tick)[0] == (('◖','◜','◖','◟')[tick%4], ('art',94,None))
        for style in ('question','next','parked','unknown'):
            assert art.sideways_acorn(7,'word',style,tick=tick)[0][0] == '◖'
