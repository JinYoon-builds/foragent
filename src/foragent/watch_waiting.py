"""Display the public waiting reader's values without interpreting their meaning."""

import unicodedata

from foragent.watch_cells import Line


def explanation(authored: dict[str, str], language: str, width: int) -> list[Line]:
    from foragent import watch

    lines = []
    for key in ("reason", "next_condition", "responsible_role"):
        value = watch.text(f"waiting_view_{key}", language, value=authored[key])
        # Keep authored spacing; only terminal control characters are removed.
        value = "  " + "".join(char for char in value if not unicodedata.category(char).startswith("C"))
        if watch._cells(value) > width:
            value = "".join(part for part, _ in watch._fit([(value, "dim")], max(0, width - 1))).rstrip() + "…"
        lines.append(watch._fit([(value, "dim")], width))
    return lines
