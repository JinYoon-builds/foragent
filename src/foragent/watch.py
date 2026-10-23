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
import importlib
import os
from pathlib import Path
import runpy
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
from foragent.watch_terminal import _terminal, color_pairs


def _installed_build() -> str:
    """Read installation metadata afresh, bypassing module and bytecode caches."""
    try:
        build = importlib.import_module("foragent._build")
        source = getattr(build, "__file__", None)
        sha = runpy.run_path(source)["GIT_SHA"] if source else build.GIT_SHA
        return sha.strip() if isinstance(sha, str) else "unknown"
    except (ImportError, OSError, SyntaxError, ValueError, KeyError, AttributeError):
        # A package replacement can briefly leave metadata missing or incomplete.
        return "unknown"


def should_reload(started: str, current: str) -> bool:
    """Only a change between two known builds warrants replacing the view."""
    return bool(started and current and started != "unknown" and current != "unknown"
                and started != current)


def run(
    status: Callable[[], dict[str, Any]], tracker: Tracker, store: StateStore,
    config: Config, repo_root: Path, *, once: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    if not once and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ConfigError(text("watch_terminal", config.language))
    argv = sys.argv.copy()
    reload_requested = False
    refresh = lambda: collect(status(), tracker, store, config, now())
    with nullcontext() if once else registered(store):
        snapshot = refresh()
        if once:
            print(render_frame(snapshot, config, repo_root))
            return
        import curses

        try:
            reload_requested = curses.wrapper(_terminal, refresh, snapshot, config, repo_root, now)
        except KeyboardInterrupt:
            pass
        except curses.error as exc:
            raise ConfigError(str(exc)) from exc
    if reload_requested:
        # Both curses and PID registration must unwind before exec replaces us.
        if Path(argv[0]).resolve() == Path(__file__).with_name("cli.py").resolve():
            os.execv(sys.executable, [sys.executable, "-m", "foragent.cli", *argv[1:]])
        else:
            os.execv(argv[0], argv)
