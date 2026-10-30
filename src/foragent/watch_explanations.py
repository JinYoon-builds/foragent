"""Dispatch an explanation target to the discovered sections that can explain it.

A section may define ``explain`` with this module's signature. It returns None for a
target that is not its own, [] when it handles the target with nothing to say, or the
rows to show. This module lives outside watch_sections so discovery never treats it
as a section.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot


def explain(target: tuple[str, int | str | None], snapshot: Snapshot, config: Config, repo_root: Path, *,
            now: datetime, width: int, state: dict[str, Any] | None) -> list[Line] | None:
    """The first non-None answer from the sections' optional ``explain``, in ORDER."""
    from foragent import watch_layout

    for section in watch_layout._sections():
        provider = getattr(section, "explain", None)
        if provider is None:
            continue
        found = provider(target, snapshot, config, repo_root, now=now, width=width, state=state)
        if found is not None:
            return found
    return None
