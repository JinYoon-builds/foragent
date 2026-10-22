"""Read-only lane conveyor, with a plain frame and a curses terminal view.

Keep the original bindings here; extracted helpers use this module's runtime
dependencies so callers can still patch every watch entry point and helper.
"""

from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice, zip_longest
import json
import os
from pathlib import Path
import sys
import time
import unicodedata
from typing import Any

from foragent.config import Config, ConfigError
from foragent.i18n import text
from foragent.labels import issue_lane, lane_display
from foragent.state import StateStore
from foragent.tracker import Issue, Tracker
from foragent.watch_cells import Line, Span, _beside, _cells, _clean, _columns, _fit, _line, _panel, _wrap
from foragent.watch_data import Snapshot, collect, recent_events
from foragent.watch_layout import PICK, _activity, _frame, _has_work, render_detail, render_frame
from foragent.watch_open import registered
from foragent.watch_terminal import _terminal


def run(
    status: Callable[[], dict[str, Any]], tracker: Tracker, store: StateStore,
    config: Config, repo_root: Path, *, once: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    if not once and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ConfigError(text("watch_terminal", config.language))
    refresh = lambda: collect(status(), tracker, store, config, now())
    with nullcontext() if once else registered(store):
        snapshot = refresh()
        if once:
            print(render_frame(snapshot, config, repo_root))
            return
        import curses

        try:
            curses.wrapper(_terminal, refresh, snapshot, config, repo_root, now)
        except KeyboardInterrupt:
            pass
        except curses.error as exc:
            raise ConfigError(str(exc)) from exc
