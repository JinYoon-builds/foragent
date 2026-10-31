"""The lane view's top line (lane-view m2): warnings, the status, today's merges and settings.

Warnings come first (the status could not be read, the gate went quiet), then the
status: a red question count, else 'running fine' or 'resting'. Today's merge count
follows, and the text button '[⚙ settings]' stands at the right edge; clicking any of
its cells opens the same settings window as the s key. When the row is too narrow,
today's count goes first, then the status is cut.
"""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.status import unknown as status_unknown
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 2
PIN = "header"
REPLACES = ("foragent.watch_sections.squirrels", "foragent.watch_sections.summary")
GAP = False
GAP_AFTER = False


def _questions(status: dict[str, Any], snapshot: Snapshot | None = None) -> int:
    """The person's questions to answer.

    With collected question facts (watch_questions): issues whose question awaits the
    person, plus open inbox questions that list no issue; an answered question counts
    no more, though its needs-human label may remain. Without them: distinct issues
    in the needs-human group. Parked work waiting for a person and open PRs also count
    as waiting_human in the summary, but they ask nothing, so the summary is not a fallback.
    """
    if snapshot is not None and snapshot.questions is not None:
        from foragent import watch_questions

        return watch_questions.waiting(snapshot.questions, snapshot.unlinked_questions)[watch_questions.PERSON]
    issues = status.get("issues")
    group = issues.get("needs-human") if isinstance(issues, dict) else None
    return len({item.get("number") for item in group or () if isinstance(item, dict)})


def _working(status: dict[str, Any]) -> bool:
    """Whether a worker is running now: a lane row in the working state (status.issue_state:
    a live run, not held by a person or blocked) or listing running issues. Queued, blocked
    and waiting work, and live runs on held issues, are pending, not running."""
    return any(row.get("state") == "working" or row.get("running")
               for row in status.get("lanes", {}).values())


def _warnings(snapshot: Snapshot, language: str, now: Any) -> list[str]:
    from foragent import watch
    from foragent.watch_sections.squirrels import GATE_STALE_SECONDS

    words = []
    if status_unknown(snapshot.status):
        words.append(watch.text("status_unknown", language))
    gate = snapshot.heartbeats.get("gate")
    if gate is not None:
        gate += max(0.0, (now - snapshot.captured_at).total_seconds())
        if gate > GATE_STALE_SECONDS:
            words.append(watch.text("watch_gate_stale", language, minutes=int(gate // 60)))
    return words


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch
    from foragent.watch_sections.lanes import glyph_mode

    status, language, width = snapshot.status, config.language, max(1, context["width"])
    hits, top = context.get("hits", []), context.get("top", 0)
    t = lambda key, **values: watch.text(key, language, **values)
    ascii_glyphs = glyph_mode(config) == "ascii"
    gear = t("lane_header_settings_ascii" if ascii_glyphs else "lane_header_settings")
    spans: Line = [(word + "  ", "yellow") for word in _warnings(snapshot, language, context["now"])]
    questions = _questions(status, snapshot)
    if questions:
        spans.append(("● " if not ascii_glyphs else "* ", "question"))
        spans.append((t("lane_header_question") if questions == 1
                      else t("lane_header_questions", count=questions), "question"))
    else:
        active = _working(status)
        spans.append(("● " if not ascii_glyphs else "* ", "green" if active else "dim"))
        spans.append((t("lane_header_running" if active else "lane_header_resting"), "bold"))
    today = snapshot.today
    today_text = t("lane_header_today_one") if today == 1 else t("lane_header_today", count=today)
    gear_cells = watch._cells(gear)
    room = max(0, width - gear_cells - 2)  # Two cells always part the status from the button.
    used = sum(watch._cells(value) for value, _ in spans)
    if used + 3 + watch._cells(today_text) <= room:
        spans.append((" · " + today_text, ""))
        used += 3 + watch._cells(today_text)
    left = watch._fit(spans, room) if used > room else [*spans, (" " * (room - used), "")]
    start = max(0, width - gear_cells)
    if room:
        line = [*left, ("  ", ""), (gear, "bold")]
    else:
        # Narrower than the button and its gap: the button alone, cut at the right edge.
        line = watch._fit([(" " * start, ""), (gear, "bold")], width)
    if start:
        hits.append((0, top, start, top + 1, ("orchestrator", None)))
    hits.append((start, top, width, top + 1, ("settings", None)))
    return [line]
