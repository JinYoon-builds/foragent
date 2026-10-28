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
import shutil
import subprocess
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
from foragent.watch_art import ORCHESTRATOR, PALETTE, half_blocks
from foragent.watch_layout import PICK, _activity, _frame, _has_work, _window, render_frame
from foragent.watch_sections.acorns import issue_box, issue_url
from foragent.watch_sections.detail import render_detail
from foragent.watch_sections.finished import FIREWORKS_SECONDS, calm
from foragent.watch_sections.stage import _stage
from foragent.watch_mouse import (
    handle_key, handle_mouse, highlighted, highlighted_row, hit_test, osc8, scroll, scroll_limit, switch_to_orchestrator,
    terminal_restore_sequences, terminal_setup_sequences, toggle_lane,
)
from foragent.watch_open import LOCK_FD_ENV, exclusive, registered
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


def _write(sequences: list[str]) -> None:
    sys.stdout.write("".join(sequences))
    sys.stdout.flush()


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
    if once:
        # A single printed frame is the calm finished scene, never fireworks.
        with calm():
            print(render_frame(refresh(), config, repo_root))
        return
    with exclusive(store) as lock:
        if lock is None:
            # Another live view holds the kernel lock: leave without registering a PID.
            print(text("watch_already_open", config.language))
            return
        with registered(store):
            snapshot = refresh()
            import curses

            # The alternate screen keeps the view out of the terminal's scrollback.
            _write(terminal_setup_sequences())
            try:
                reload_requested = curses.wrapper(_terminal, refresh, snapshot, config, repo_root, now, store)
            except KeyboardInterrupt:
                pass
            except curses.error as exc:
                raise ConfigError(str(exc)) from exc
            finally:
                # Leave the alternate screen on exit and before a re-exec alike.
                _write(terminal_restore_sequences())
        if not reload_requested:
            return
        # Curses and PID registration unwind before exec; the inherited lock leaves no gap.
        os.environ[LOCK_FD_ENV] = str(lock)
        if Path(argv[0]).resolve() == Path(__file__).with_name("cli.py").resolve():
            os.execv(sys.executable, [sys.executable, "-m", "foragent.cli", *argv[1:]])
        else:
            os.execv(argv[0], argv)
