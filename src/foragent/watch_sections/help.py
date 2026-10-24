"""The key help line at the bottom of the frame."""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 60
GAP = False  # The help line sits directly under the section above it.


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    count = len(snapshot.status["lanes"])
    key = "watch_help_many" if count > 9 else "watch_help"
    return [watch._line(watch.text(key, config.language, count=count))]
