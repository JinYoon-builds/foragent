"""The dreaming lane's row (#190): 💭 <lane> dreaming · ✓ N done, in place of its 💤 row.

This module holds the helper the lane rows (watch_sections/lanes) call. Every module
in this package is discovered as a section, so it keeps an ORDER and a ``render``
that draws nothing; ``GAP = False`` keeps it from adding a blank line, so the dream
adds no screen row. The view trusts the dream record in the snapshot: the tick owns
its liveness.
"""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 7
GAP = False  # It draws nothing; it must not cost a blank row.

GLYPHS = {"emoji": "💭", "ascii": "[~]"}


def idle_row(row: dict[str, Any], glyph: str, mode: str, rest: str, language: str) -> dict[str, Any]:
    """A rested lane's summary row: 💤 with its done count, or 💭 while its dream record runs."""
    from foragent import watch

    if not row.get("dreaming"):
        return {"glyph": glyph, "lane": row["lane_display"], "rest": rest}
    words = watch.text("dream_lane", language)
    return {"glyph": GLYPHS[mode], "lane": row["lane_display"],
            "rest": f"{words} · {rest}" if rest else words}


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    return []  # The dreaming row is drawn by the lane rows (watch_sections/lanes).
