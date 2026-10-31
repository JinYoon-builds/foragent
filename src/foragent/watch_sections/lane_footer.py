"""The lane view's two bottom rows (lane-view m2): the usage row, then the key hints.

The usage row reuses the usage line's parts without the concurrency count (memory
warnings stay); it carries no invite, merge summary or quit item, since the hints
row names q. Both rows are cut to the width, never wrapped, so the hints stay last.
"""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot
from foragent.watch_sections.usage import _parts

ORDER = 100
PIN = "footer"
REPLACES = ("foragent.watch_sections.usage", "foragent.watch_sections.help")
GAP = False
GAP_AFTER = False


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    width = max(1, context.get("width") or 120)
    parts, _ = _parts(snapshot, config, context, include_capacity=False)
    usage = watch._fit(watch._line(" · ".join(parts)), width)
    hints = watch._fit(watch._line(watch.text("lane_footer_hints", config.language), "dim"), width)
    return [usage, hints]
