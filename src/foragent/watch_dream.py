"""The dreaming lane's row (#190): 💭 <lane> dreaming · ✓ N done, in place of its 💤 row.

The lane rows (watch_sections/lanes) call this helper. It lives outside the
watch_sections package because every module there is discovered as a screen
section, and a section that draws nothing would still cost a row whenever a frame
draws every section. The view trusts the dream record in the snapshot: the tick owns
its liveness.
"""

from typing import Any

GLYPHS = {"emoji": "💭", "ascii": "[~]"}


def idle_row(row: dict[str, Any], glyph: str, mode: str, rest: str, language: str) -> dict[str, Any]:
    """A rested lane's summary row: 💤 with its done count, or 💭 while its dream record runs."""
    from foragent import watch

    if not row.get("dreaming"):
        return {"glyph": glyph, "lane": row["lane_display"], "rest": rest}
    words = watch.text("dream_lane", language)
    return {"glyph": GLYPHS[mode], "lane": row["lane_display"],
            "rest": f"{words} · {rest}" if rest else words}
