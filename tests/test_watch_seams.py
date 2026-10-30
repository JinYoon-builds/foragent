"""Unit checks for the lane-view seams: painter palette, selection keys and art builders."""

from types import SimpleNamespace

from foragent import watch_art, watch_terminal
from foragent.config import Config


def fake_curses(*, colors=256, pairs=64, has_colors=True):
    import curses

    initialized = []
    fake = SimpleNamespace(
        A_BOLD=curses.A_BOLD, A_DIM=curses.A_DIM, A_REVERSE=curses.A_REVERSE,
        COLOR_RED=curses.COLOR_RED, COLOR_WHITE=curses.COLOR_WHITE, COLORS=colors,
        has_colors=lambda: has_colors, color_pair=lambda index: index << 8,
        init_pair=lambda index, fg, bg: initialized.append((index, fg, bg)),
    )
    if pairs is not None:
        fake.COLOR_PAIRS = pairs
    return fake, initialized


def test_structured_pairs_are_cached_and_stop_at_the_pair_limit():
    curses, initialized = fake_curses(pairs=13)
    palette = watch_terminal._Palette(curses, {})
    first = palette.attribute(("art", 94, None))
    assert palette.attribute(("art", 94, None)) == first  # Cached, never reinitialized.
    palette.attribute(("art", 232, 220))
    assert palette.attribute(("art", 1, 2)) == curses.A_BOLD  # Pairs 11 and 12 used up: no colour.
    assert initialized == [(11, 94, -1), (12, 232, 220)]


def test_eight_colour_terminal_uses_the_fixed_lookup():
    curses, initialized = fake_curses(colors=8)
    watch_terminal._Palette(curses, {}).attribute(("art", 232, 220))
    assert initialized == [(11, 0, 3)]


def test_colourless_terminal_keeps_text_bold_and_dim_without_colour_codes():
    curses, initialized = fake_curses(has_colors=False, pairs=None)
    styles = {}
    palette = watch_terminal._Palette(curses, styles)
    assert palette.attribute(("art", 232, 220)) == curses.A_BOLD
    assert palette.attribute("next") == curses.A_DIM
    assert palette.link(("art", 232, 220), curses.A_BOLD) == "\x1b[0m\x1b[1m"
    assert initialized == []


def test_named_styles_register_once_and_links_keep_their_colour():
    curses, initialized = fake_curses(colors=8)
    palette = watch_terminal._Palette(curses, {})
    attribute = palette.attribute("next")
    palette.attribute("next")
    assert initialized == [(9, curses.COLOR_WHITE, -1)]
    assert attribute == (9 << 8) | curses.A_DIM
    assert palette.link("next", attribute) == f"\x1b[0m\x1b[2;38;5;{curses.COLOR_WHITE}m"


def test_selection_keys_toggle_and_clear_without_touching_the_issue_box(tmp_path):
    from foragent.state import StateStore
    from foragent.watch import handle_key, handle_mouse

    store, config = StateStore(tmp_path / "state"), Config()
    layout = {"hits": [(0, 0, 3, 1, ("squirrel", "docs")), (0, 1, 3, 2, ("more", "docs"))]}
    state = {"scroll": 4}
    handle_mouse("left", 0, 0, layout, store, config, state=state)
    assert state["explanation_target"] == ("squirrel", "docs")
    handle_mouse("left", 0, 0, layout, store, config, state=state)
    assert state.get("explanation_target") is None  # The same target toggles off.
    handle_mouse("left", 0, 0, layout, store, config, state=state)
    handle_mouse("left", 0, 1, layout, store, config, state=state)
    assert state.get("explanation_target") is None and state["unfolded"] == {"docs"}  # Cleared, then acted.
    handle_mouse("left", 0, 0, layout, store, config, state=state)
    assert handle_key("x", store, config, state=state) is False
    assert state.get("explanation_target") is None and state["scroll"] == 4 and state.get("issue") is None
    assert handle_key("x", store, config, state=state) is False  # Nothing open: x does nothing.


def test_sideways_acorn_canonicalizes_styles_and_never_looks_running():
    assert watch_art.sideways_acorn(3, "", "yellow") == watch_art.sideways_acorn(3, "", "working")
    parked = watch_art.sideways_acorn(3, "parked", "parked")
    unknown = watch_art.sideways_acorn(3, "parked", "mystery")
    assert parked == unknown and parked[2] == ("#3 parked", ("art", 252, 240))
    assert watch_art.sideways_acorn(3, "", "question", ascii=True) == [("[#3]", "question")]
    assert watch_art.sideways_acorn(3, "x", "parked", ascii=True) == [("[#3 x]", "next")]
