"""The fireworks band above the finished-order list (#168).

When every child of the order is closed, three rows of coloured ``*`` and ``+``
play for FIREWORKS_SECONDS, counted in view ticks from the tick that first sees
this order finished, then the band disappears. It plays once per finished order
per view process: ``state["fireworks"]`` remembers the first tick per order
number. Without a state it counts from tick 0. Inside ``calm()`` (fa gate watch
--once prints one still frame) and with ``animation: false`` it never plays. The band needs no strings.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot
from foragent.watch_sections.done import finished

ORDER = 22
GAP_AFTER = False  # The band sits just above the done list's completion sentence.
# On a short screen the band gives way before the done list and the lane rows.
YIELD_BEFORE = "done"
FIREWORKS_SECONDS = 3
TICK_SECONDS = 0.3  # The live view's animation step.
MIN_WIDTH, MIN_HEIGHT = 40, 16
ROWS = 3
COLOURS = ("yellow", "green", "claude", "blue")
# render_frame's arguments are a fixed interface, so the one-shot print says "still" here.
_CALM: ContextVar[bool] = ContextVar("foragent_watch_calm", default=False)


@contextmanager
def calm() -> Iterator[None]:
    """Frames drawn inside show the calm finished scene, never fireworks."""
    token = _CALM.set(True)
    try:
        yield
    finally:
        _CALM.reset(token)


def playing(snapshot: Snapshot, config: Config, tick: int, state: dict[str, Any] | None) -> bool:
    """Whether the fireworks show at this tick; records the order's first finished tick."""
    if not config.animation or _CALM.get() or not finished(snapshot):
        return False
    if state is None:
        start = 0
    else:
        order = getattr(snapshot.order, "number", None)
        start = state.setdefault("fireworks", {}).setdefault(order, tick)
    return 0 <= (tick - start) * TICK_SECONDS < FIREWORKS_SECONDS


def band(tick: int, width: int) -> list[Line]:
    """Three rows of bursts that move every tick."""
    lines: list[Line] = []
    for row in range(ROWS):
        spans: list[tuple[str, str]] = []
        for column in range(width):
            seed = (column * 7 + row * 13 + tick * 5) % 23
            if seed in (0, 11):
                spans.append(("*" if seed == 0 else "+", COLOURS[(column + row + tick) % len(COLOURS)]))
            else:
                spans.append((" ", ""))
        lines.append(spans)
    return lines


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    width, height = context["width"], context.get("height")
    # The timer starts on the first finished tick even when the band does not fit.
    if not playing(snapshot, config, context["tick"], context.get("state")):
        return []
    if width < MIN_WIDTH or (height is not None and height < MIN_HEIGHT):
        return []
    return band(context["tick"], width)
