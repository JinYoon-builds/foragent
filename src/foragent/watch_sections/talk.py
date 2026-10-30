"""Talking to a lane's worker from the view (#174): the squirrel click and its message line.

A click on a lane row's squirrel (its glyph cell) runs the same ``lanes.talk`` as
``fa lanes talk`` for the lane's current issue, else its last one (the most recently
finished run, ``last_issue`` in status). A lane with neither does nothing. The result
or error stays on one line under the lane rows until the next key or 10 seconds.
"""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 8  # Directly under the lane rows (5) and the open issue box (6).
GAP = False
# The result must be seen before a key or the timeout: long lane rows never trim it away,
# and it moves up into the first screen when they push it below.
STAYS = True
# How long the talk result stays on screen without a key.
MESSAGE_SECONDS = 10
# Below this width a talking row names its state in words instead of a badge line above it.
BADGE_WIDTH = 44
# Row states whose issue is the lane's current one; any other row talks to the last issue.
CURRENT_STATES = frozenset({"working", "blocked", "waiting-human"})


def lane_issue(row: dict[str, Any] | None) -> int | None:
    """The issue a lane's squirrel talks to: the current one, else the last finished, else None."""
    if not row:
        return None
    if row.get("issue") is not None and row.get("state") in CURRENT_STATES:
        return row["issue"]
    return row.get("last_issue")


def talk(number: int, config: Config, repo_root: Path, store: Any = None) -> str:
    """Run ``fa lanes talk <number>`` in process; return its line or the LaneError text.

    ``lanes.talk`` returns once the terminal is opened, so the view never waits on the talk.
    """
    from foragent import lanes
    from foragent.git import GitRepo
    from foragent.state import StateStore

    root = Path(repo_root).resolve()
    if store is None:
        store = StateStore.for_repo(config.repo or GitRepo(root).remote_slug())
    try:
        return lanes.talk(number, message=None, store=store, config=config, repo_root=root)
    except lanes.LaneError as exc:
        return str(exc)


def show(state: dict[str, Any] | None, line: str) -> None:
    """Keep the talk result for the message line."""
    from foragent import watch

    if state is not None:
        state["talk_message"] = (line, watch.time.monotonic())


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    state = context.get("state")
    found = (state or {}).get("talk_message")
    if not found:
        return []
    line, at = found
    if watch.time.monotonic() - at > MESSAGE_SECONDS:
        state.pop("talk_message", None)
        return []
    return [watch._fit(watch._line(watch._clean(line), "yellow"), context["width"])]


def alert(state: dict[str, Any] | None, visible: list[Line], width: int) -> list[Line]:
    """The talk line for the live view's bottom message row when the screen does not show it.

    A screen shorter than the frame, or scrolled, can leave the line under the lane rows
    off screen; the bottom row then carries it until the next key or the timeout.
    """
    from foragent import watch

    found = (state or {}).get("talk_message")
    if not found:
        return []
    line = watch._fit(watch._line(watch._clean(found[0]), "yellow"), width)
    shown = "".join(value for value, _ in line).rstrip()
    if any("".join(value for value, _ in row).rstrip() == shown for row in visible):
        return []
    return [line]
