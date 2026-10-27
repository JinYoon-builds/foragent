"""The finished-order section: a completion sentence and the merged issues by lane.

While the order still runs it is one line: the finished count and the newest title.
"""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 22
SHOWN = 8


def finished(snapshot: Snapshot) -> bool:
    """An order is done when every child counted in the progress bar is closed."""
    return snapshot.total > 0 and snapshot.completed == snapshot.total


def _newest_first(snapshot: Snapshot) -> list[tuple[Any, str]]:
    """Every merged issue with its lane: newest merge first, unknown times last by number."""
    items = [(issue, slug) for slug, issues in snapshot.done.items() for issue in issues]
    known = sorted((item for item in items if item[0].number in snapshot.merged_at),
                   key=lambda item: (snapshot.merged_at[item[0].number], item[0].number), reverse=True)
    unknown = sorted((item for item in items if item[0].number not in snapshot.merged_at),
                     key=lambda item: item[0].number, reverse=True)
    return known + unknown


def _item(number: int, title: str, lane: str, language: str, width: int) -> list[Line]:
    """One issue; the title gives way first, with an ellipsis, so the number and lane stay.

    When even an empty title leaves no room (a very long lane name), the line wraps
    between words instead of being cut.
    """
    from foragent import watch

    value = watch.text("done_state_item", language, number=number, title=title, lane=lane)
    if watch._cells(value) > width:
        bare = watch.text("done_state_item", language, number=number, title="", lane=lane)
        room = width - watch._cells(bare) - 1
        if room > 0:
            cut = "".join(part for part, style in watch._fit(watch._line(title), room)).rstrip()
            value = watch.text("done_state_item", language, number=number, title=cut + "…", lane=lane)
    return _wrapped(value, width)


def _wrapped(value: str, width: int, style: str = "") -> list[Line]:
    """The text on one line when it fits, else wrapped between words; never cut."""
    from foragent import watch

    return [watch._line(value, style)] if watch._cells(value) <= width else watch._wrap(value, width, style)


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    if not finished(snapshot):
        # While the order runs, one line names the count and the newest finished issue.
        items = _newest_first(snapshot)
        if not items:
            return []
        line = watch.text("watch_done", config.language, count=len(items), title=watch._clean(items[0][0].title))
        return [watch._fit(watch._line(line, "green"), context["width"])]
    language, width = config.language, context["width"]
    lines = _wrapped(watch.text("done_state_heading", language), width, "green")
    items = _newest_first(snapshot)
    for issue, slug in items[:SHOWN]:
        lane = watch.lane_display(config, slug, language)
        lines += _item(issue.number, watch._clean(issue.title), lane, language, width)
    if len(items) > SHOWN:
        lines += _wrapped(watch.text("done_state_more", language, count=len(items) - SHOWN), width)
    return lines

