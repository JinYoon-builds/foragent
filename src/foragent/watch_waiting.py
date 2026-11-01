"""Display the public waiting reader's values without interpreting their meaning."""

import unicodedata

from foragent.watch_cells import Line


def explanation(authored: dict[str, str], language: str, width: int, *, mark: str = "…") -> list[Line]:
    from foragent import watch
    from foragent.watch_scene import fit_marked

    def printable(value: str) -> str:
        # Tabs have visible spacing; other terminal controls must not steer the screen.
        return "".join(char for char in value.expandtabs(8) if not unicodedata.category(char).startswith("C"))

    keys = ("reason", "next_condition", "responsible_role")
    # A partial wait shows its recorded text; a wait with no text keeps the heading alone.
    values = ([watch.text(f"waiting_view_{key}", language, value=printable(authored[key])) for key in keys]
              if all(isinstance(authored.get(key), str) for key in keys) else
              [watch.text("waiting_view_heading", language),
               *[printable(line) for line in (authored.get("text") or "").splitlines()]])
    lines = []
    for value in values:
        value = "  " + value
        lines.append(fit_marked([(value, "dim")], width, mark))
    return lines
