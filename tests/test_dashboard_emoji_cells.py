"""Emoji presentation (VS16) is two terminal cells, and a cut never splits the pair."""

from foragent import watch


def test_vs16_emoji_counts_two_cells():
    assert watch._cells("\U0001f6e0️") == 2
    assert watch._cells("⚙️ a") == 4
    assert watch._cells("\U0001f6e0") == 1  # without VS16 the Unicode width stands
    assert watch._cells("가") == 2


def test_fit_keeps_the_emoji_and_its_selector_together():
    fitted = watch._fit([("⚙️ Web", "")], 1)
    assert fitted[0][0] == ""  # a two-cell emoji does not fit one cell
    fitted = watch._fit([("⚙️ Web", "")], 4)
    assert "".join(value for value, _ in fitted) == "⚙️ W"
