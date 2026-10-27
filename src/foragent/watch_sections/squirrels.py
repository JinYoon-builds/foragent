"""The header above the lane rows: the one big orchestrator squirrel, or one text line (#167).

The squirrel shows on the first run (no run ever recorded) or when every lane is
idle, unless the D76 speech stage below already draws it. Otherwise the header
is one line: the orchestrator's latest say line, or today's counts.
"""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.status import summary_values, unknown as status_unknown
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 3
KEEP = True  # The header stays when the screen is short.
# The gate runs every few minutes; this long without its heartbeat is a warning (D67).
GATE_STALE_SECONDS = 15 * 60


def latest_say(snapshot: Snapshot) -> str | None:
    """The first line of the newest say in the journal, by its timestamp."""
    events = [event for event in [*snapshot.events, snapshot.say] if event
              and isinstance(event.get("say"), str) and event["say"].strip()]
    if not events:
        return None
    newest = max(events, key=lambda event: str(event.get("at") or ""))
    return newest["say"].strip().splitlines()[0]


def _counts(snapshot: Snapshot, language: str) -> str:
    from foragent import watch

    values = summary_values(snapshot.status["summary"])
    waiting = values["blocked"], values["waiting_human"]
    return watch.text("squirrel_header", language, done=snapshot.today, working=values["working"],
                      waiting="?" if "?" in waiting else sum(waiting), queued=values["queued"])


def _cut(value: str, width: int, style: str) -> Line:
    """One line cut to the width with an ellipsis."""
    from foragent import watch

    value = watch._clean(value)
    if watch._cells(value) > width:
        value = "".join(part for part, _ in watch._fit(watch._line(value), max(0, width - 1))).rstrip() + "…"
    return watch._line(value, style)


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    status, language, width = snapshot.status, config.language, context["width"]
    top, hits = context.get("top", 0), context.get("hits", [])
    words, style = [], "bold"
    if status_unknown(status):
        words.append(watch.text("status_unknown", language))
        style = "yellow"
    gate = snapshot.heartbeats.get("gate")
    if gate is not None:
        gate += max(0.0, (context["now"] - snapshot.captured_at).total_seconds())
        if gate > GATE_STALE_SECONDS:
            words.append(watch.text("watch_gate_stale", language, minutes=int(gate // 60)))
            style = "yellow"
    say = latest_say(snapshot)
    first_run = not status.get("history") and not status["active_runs"]
    idle = not status["active_runs"] and not any(watch._has_work(row) for row in status["lanes"].values())
    staged = any(row.get("status") == "processing" for row in snapshot.requests)
    if (first_run or idle) and not staged:
        art: list[Line] = [[(glyph, "art") for glyph, foreground, background in row]
                           for row in watch.half_blocks(watch.ORCHESTRATOR, watch.PALETTE)]
        if say:
            words.append(say)
        if words and width >= 56:
            art[1] = [*art[1], ("  ", ""), *_cut(" · ".join(words), width - 20, style)]
        hits.append((0, top, min(width, 18), top + len(art), ("orchestrator", None)))
        return art + [[]]
    words.append(say or _counts(snapshot, language))
    hits.append((0, top, width, top + 1, ("orchestrator", None)))
    return [_cut(" · ".join(words), width, style)]
