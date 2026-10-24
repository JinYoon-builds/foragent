"""The big orchestrator squirrel with the planning request it is handling (D76)."""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 20


def _stage(snapshot: Snapshot, language: str, width: int) -> list[Line]:
    """The big orchestrator squirrel saying which planning request it is handling (D76).

    Shown only while a request in the durable box is being processed. The speech
    shares the squirrel's pixel rows from 56 columns, sits below it from 44, and
    is plain text below that.
    """
    from foragent import watch

    processing = [row for row in snapshot.requests if row.get("status") == "processing"]
    if not processing:
        return []
    request = (processing[-1].get("text") or "").strip().splitlines() or [""]
    speech = watch.text("watch_request_bubble", language, text=request[0])
    if width < 44:
        return [watch._fit(watch._line(speech, "bold"), width)]
    art: list[Line] = [[(glyph, "art") for glyph, foreground, background in row]
                       for row in watch.half_blocks(watch.ORCHESTRATOR, watch.PALETTE)]
    if width < 56:
        return art + [watch._fit(watch._line(speech, "bold"), width)]
    art[1] = [*art[1], ("  ", ""), *watch._fit(watch._line(speech, "bold"), width - 20)]
    return art


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    width, top = context["width"], context.get("top", 0)
    stage = watch._stage(snapshot, config.language, width)
    if stage:
        context.get("hits", []).append(
            (0, top, width if width < 44 else 18, top + len(stage), ("orchestrator", None)))
    return stage
