"""Display the public waiting reader's values without interpreting their meaning."""

import unicodedata

from foragent.watch_cells import Line


def explanation(authored: dict[str, str], language: str, width: int) -> list[Line]:
    from foragent import watch

    def printable(value: str) -> str:
        # Tabs have visible spacing; other terminal controls must not steer the screen.
        return "".join(char for char in value.expandtabs(8) if not unicodedata.category(char).startswith("C"))

    keys = ("reason", "next_condition", "responsible_role")
    values = ([watch.text(f"waiting_view_{key}", language, value=printable(authored[key])) for key in keys]
              if all(key in authored for key in keys) else
              [watch.text("waiting_view_heading", language), *[printable(line) for line in authored["text"].splitlines()]])
    lines = []
    for value in values:
        value = "  " + value
        if watch._cells(value) > width:
            value = "".join(part for part, _ in watch._fit([(value, "dim")], max(0, width - 1))).rstrip() + "…"
        lines.append(watch._fit([(value, "dim")], width))
    return lines
