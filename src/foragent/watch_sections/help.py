"""The key help line at the bottom of the frame."""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 60
GAP = False  # The help line sits directly under the section above it.
KEEP = True  # The footer stays when the screen is short.


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    if context.get("short"):
        # Sections gave way to the lane rows: one short line of the key letters.
        return [watch._line(watch.text("acorns_keys", config.language))]
    count = len(snapshot.status["lanes"])
    # The lane-row keys (#170) live with their feature in messages/acorns.py.
    key = "acorns_help_many" if count > 9 else "acorns_help"
    return [watch._line(watch.text(key, config.language, count=count))]
