"""Terminal cell widths measured independently of Foragent's own cell code.

A test that checks "fits the width" with the product's own measure would pass even when
that measure is wrong; this one follows Unicode's East Asian Width directly.
"""

import unicodedata


def cells(text: str) -> int:
    """Cells a terminal gives the text: wide and fullwidth characters take two, combining
    marks and other zero-width characters none, everything else one."""
    total = 0
    for char in text:
        if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total
