"""The big orchestrator squirrel, its eight-row speech bubble and the r57 progress bar (lane-view m2).

The bubble says what the lanes are doing (work, waiting, the finish estimate, questions,
the planning request being handled, today's newest merges and a hint), or explains the
selected target through watch_explanations; acorns are explained here. From 80 columns
the sprite stands left of the bubble with its mouth on row 3; narrower, or without
pixels (ASCII, a non-UTF-8 terminal or animation off), the bubble takes the whole width.
The bar under the bubble counts today's merges against them plus the approved work left.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 10
REPLACES = ("foragent.watch_sections.stage", "foragent.watch_sections.progress",
            "foragent.watch_sections.orch_panel", "foragent.watch_sections.activity")
GAP = False
GAP_AFTER = False

BODY_ROWS = 6
SPRITE_CELLS = 19  # The 18-pixel orchestrator, padded by one cell.
SPRITE_GAP = 2
SPRITE_FROM = 80  # The narrowest frame that shows the sprite beside the bubble.
MOUTH_ROW = 3
BAR_CELLS = 20
MERGES_SHOWN = 4
# Status issue groups of approved work that is not merged yet (r57).
REMAINING = ("running", "ready", "approved-waiting", "needs-human", "pr-open")


def _pixels(config: Config) -> bool:
    from foragent.watch_sections.lanes import glyph_mode

    return config.animation and glyph_mode(config) != "ascii"


def _ellipsis(config: Config) -> str:
    from foragent.watch_sections.lanes import glyph_mode

    return "..." if glyph_mode(config) == "ascii" else "…"


def _cut(value: str, width: int, mark: str) -> str:
    """One row of at most ``width`` cells, ending in ``mark`` when anything was cut."""
    from foragent import watch

    value = watch._clean(value)
    if watch._cells(value) <= width:
        return value
    room = max(0, width - watch._cells(mark))
    return "".join(part for part, _ in watch._fit([(value, "")], room)).rstrip() + mark


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def latest_merges(snapshot: Snapshot) -> list[int]:
    """The four newest distinct merges from local midnight through the capture time, newest first.

    Ties keep the map's order; a hand-built map obeys the same window as a collected one.
    """
    captured = _aware(snapshot.captured_at)
    midnight = captured.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    timed = [(number, _aware(moment)) for number, moment in snapshot.merged_at.items()
             if isinstance(moment, datetime) and midnight <= _aware(moment) <= captured]
    timed.sort(key=lambda pair: pair[1], reverse=True)  # Stable: equal times keep their order.
    return [number for number, _ in timed[:MERGES_SHOWN]]


def bar_counts(snapshot: Snapshot) -> tuple[int, int]:
    """(done, total): today's merges, and those plus the distinct approved issues left."""
    issues = snapshot.status.get("issues")
    left = {item.get("number") for name in REMAINING
            for item in ((issues or {}).get(name) or () if isinstance(issues, dict) else ())
            if isinstance(item, dict) and item.get("number") is not None}
    done = max(0, int(snapshot.today or 0))
    return done, done + len(left)


def _bar(snapshot: Snapshot, language: str, width: int) -> list[Line]:
    from foragent import watch

    done, total = bar_counts(snapshot)
    if not total:
        return []
    percent = round(100 * done / total)
    label = (watch.text("lane_bubble_bar_one", language, percent=percent, done=done) if total == 1
             else watch.text("lane_bubble_bar", language, percent=percent, done=done, total=total))
    cells = max(0, min(BAR_CELLS, width - watch._cells(label) - 2))
    filled = round(cells * done / total)
    return [[("█" * filled, "green"), ("░" * (cells - filled), "dim"), ("  " if cells else "", ""), (label, "")]]


def _clock(at: datetime, now: datetime, language: str) -> str:
    """HH:MM, with the existing date words (progress._when) when it falls on a later day."""
    from foragent.watch_sections import progress

    local = at.astimezone()
    return progress._when(at, now, language, f"{local.hour:02}:{local.minute:02}")


def summary(snapshot: Snapshot, config: Config, now: datetime) -> list[tuple[str, str, str]]:
    """The default sentences in order, as (role, text, style)."""
    from foragent import watch, watch_scene

    status, language = snapshot.status, config.language
    t = lambda key, **values: watch.text(key, language, **values)
    rows = list(status.get("lanes", {}).values())
    sentences: list[tuple[str, str, str]] = []
    issues = sum(len(row.get("running") or ()) for row in rows)
    lanes = sum(bool(row.get("running")) for row in rows)
    if issues:
        key = ("lane_bubble_working_one_each" if issues == 1 and lanes == 1
               else "lane_bubble_working_one_issue" if issues == 1
               else "lane_bubble_working_one_lane" if lanes == 1 else "lane_bubble_working")
        sentences.append(("work", t(key, issues=issues, lanes=lanes), ""))
    else:
        sentences.append(("idle", t("lane_bubble_idle"), ""))
    queued = sum(len(row.get("queued") or ()) for row in rows)
    if queued:
        sentences.append(("queued", t("lane_bubble_queued_one") if queued == 1
                          else t("lane_bubble_queued", count=queued), ""))
    if snapshot.eta is not None:
        low, high = snapshot.eta_range or (snapshot.eta, snapshot.eta)
        low, high = _clock(low, now, language), _clock(high, now, language)
        sentences.append(("eta", t("lane_bubble_eta", low=low) if low == high
                          else t("lane_bubble_eta_range", low=low, high=high), ""))
        if snapshot.recent > 0:
            sentences.append(("pace", t("lane_bubble_pace_one") if snapshot.recent == 1
                              else t("lane_bubble_pace", count=snapshot.recent), ""))
    asked = len(watch_scene.questions(status))
    if asked:
        sentences.append(("questions", t("lane_bubble_questions_one") if asked == 1
                          else t("lane_bubble_questions", count=asked), "question"))
    processing = [row for row in snapshot.requests if row.get("status") == "processing"]
    if processing:
        # The planning request being handled now (D76), its first line only.
        request = (processing[-1].get("text") or "").strip().splitlines() or [""]
        sentences.append(("request", t("watch_request_bubble", text=request[0]), "bold"))
    merged = latest_merges(snapshot)
    if merged:
        sentences.append(("merges", t("lane_bubble_merges", ids=", ".join(f"#{n}" for n in merged)), ""))
    sentences.append(("hint", t("lane_bubble_hint"), "dim"))
    return sentences


def _summary_rows(sentences: list[tuple[str, str, str]], width: int, mark: str) -> list[Line]:
    """Wrap each sentence on its own rows (the pace follows the estimate), within six rows.

    On overflow the hint goes first, then the planning request, then the pace; what still
    does not fit is cut after the sixth row, which ends in an ellipsis.
    """
    from foragent import watch

    def wrapped(kept: list[tuple[str, str, str]]) -> list[Line]:
        rows: list[Line] = []
        for index, (role, value, style) in enumerate(kept):
            if role == "pace":
                continue
            if index + 1 < len(kept) and kept[index + 1][0] == "pace":
                value += " " + kept[index + 1][1]
            rows += watch._wrap(value, width, style)
        return rows

    kept = list(sentences)
    rows = wrapped(kept)
    for role in ("hint", "request", "pace"):
        if len(rows) <= BODY_ROWS:
            break
        kept = [item for item in kept if item[0] != role]
        rows = wrapped(kept)
    return _clip(rows, width, mark)


def _clip(rows: list[Line], width: int, mark: str) -> list[Line]:
    if len(rows) <= BODY_ROWS:
        return rows
    from foragent import watch

    last = rows[BODY_ROWS - 1]
    text = watch._clean("".join(value for value, _ in last))
    style = last[0][1] if last else ""
    # The sixth row always ends in the mark: something was left out, even when it was full.
    room = max(0, width - watch._cells(mark))
    kept = "".join(part for part, _ in watch._fit([(text, "")], room)).rstrip()
    return rows[:BODY_ROWS - 1] + [[(kept + mark, style)]]


def _explanation_rows(found: list[Line], width: int, mark: str) -> list[Line]:
    """Each logical row of an explanation wrapped by cells, within six rows."""
    from foragent import watch

    rows: list[Line] = []
    for line in found:
        text = "".join(value for value, _ in line)
        style = next((style for value, style in line if value.strip()), "")
        rows += watch._wrap(text, width, style) if text.strip() else [[]]
    return _clip(rows, width, mark)


# Box glyphs: corners (top left, top right, bottom left, bottom right), edge, side and mouth.
BOX = {"emoji": ("╭╮╰╯", "─", "│", "┤"), "ascii": ("++++", "-", "|", "|")}


def _box(body: list[Line], width: int, mouth: bool, glyphs: str) -> list[Line]:
    from foragent import watch

    corners, edge, side, opening = BOX[glyphs]
    inside = max(0, width - 4)
    rows = [[(corners[0] + edge * max(0, width - 2) + corners[1], "edge")]]
    for index in range(BODY_ROWS):
        line = body[index] if index < len(body) else []
        left = opening if mouth and index + 1 == MOUTH_ROW else side
        rows.append([(left, "edge"), (" ", ""), *watch._fit(line, inside), (" ", ""), (side, "edge")])
    rows.append([(corners[2] + edge * max(0, width - 2) + corners[3], "edge")])
    return rows


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch, watch_explanations
    from foragent.watch_art import ORCHESTRATOR, pixel_rows

    width, top, now = max(1, context["width"]), context.get("top", 0), context["now"]
    state = context.get("state")
    hits = context.get("hits", [])
    mark = _ellipsis(config)
    sprite = _pixels(config) and width >= SPRITE_FROM
    box_width = width - SPRITE_CELLS - SPRITE_GAP if sprite else width
    inside = max(1, box_width - 4)
    target = state.get("explanation_target") if state is not None else None
    found = (watch_explanations.explain(target, snapshot, config, repo_root, now=now, width=inside, state=state)
             if target is not None else None)
    body = (_explanation_rows(found, inside, mark) if found is not None
            else _summary_rows(summary(snapshot, config, now), inside, mark))
    if width < 4:
        # Too narrow for borders: the sentences alone, cut to the width.
        return [watch._fit(line, width) for line in body if line] + _bar(snapshot, config.language, width)
    from foragent.watch_sections.lanes import glyph_mode

    box = _box(body, box_width, sprite, glyph_mode(config))
    if sprite:
        art = pixel_rows(ORCHESTRATOR)
        box = [[*art[index], (" " * (SPRITE_CELLS - len(art[index]) + SPRITE_GAP), ""), *row]
               for index, row in enumerate(box)]
        hits.append((0, top, SPRITE_CELLS, top + len(box), ("orchestrator", None)))
    return box + _bar(snapshot, config.language, width)


def explain(target: tuple[str, int | str | None], snapshot: Snapshot, config: Config, repo_root: Path, *,
            now: datetime, width: int, state: dict | None) -> list[Line] | None:
    """An acorn's rows: #N and title (one row), its lane, what it is doing and for how long,
    and a worker warning when there is one. Other targets are not this section's."""
    from foragent import watch, watch_scene
    from foragent.status import _age
    from foragent.watch_sections.lanes import elapsed_text

    if not (isinstance(target, tuple) and len(target) == 2 and target[0] == "acorn" and type(target[1]) is int):
        return None
    number, language = target[1], config.language
    t = lambda key, **values: watch.text(key, language, **values)
    needs = watch_scene.questions(snapshot.status)
    for row in snapshot.status.get("lanes", {}).values():
        current, upcoming = watch_scene.lane_items(snapshot, row, needs)
        item = next((item for item in current + upcoming if item["number"] == number), None)
        if item is not None:
            break
    else:
        return None  # Not an acorn on screen: the summary stays.
    slug, name = row.get("slug"), row.get("lane_display") or row.get("slug") or ""
    facts = snapshot.lane_facts.get(slug)
    paths = facts.get("owned_paths") if isinstance(facts, dict) else (config.lanes.get(slug) or {}).get("paths")
    paths = [str(path) for path in paths or ()]
    rows: list[Line] = [[(_cut(f"#{number} {item['title']}".rstrip(), width, _ellipsis(config)), "bold")]]
    rows.append([(t("lane_bubble_lane_paths", lane=name, paths=", ".join(paths)) if paths
                  else t("lane_bubble_lane", lane=name), "")])
    kind, run, phase = item["class"], item["run"], item["phase"]
    if kind == "question":
        return rows + [[(t("lane_bubble_question_click"), "question")]]
    if kind == "parked":
        return rows + [[(t("lane_bubble_parked_click", lane=name), "")]]
    if kind == "next":
        rows.append([(t("lane_bubble_queued_click", lane=name), "")])
        reason = watch_scene.waiting_reason(snapshot, row, number, language)
        if reason != t("lane_blocks_wait"):
            rows.append([(reason, "")])
        return rows
    from foragent import i18n

    if kind == "wait":
        key = "blocked" if item["state"] == "blocked" and phase not in watch_scene.WAIT_PHASES else phase
        known = key in i18n.MESSAGES.get(language, i18n.MESSAGES["en"])
        rows.append([(t(key) if known else t("lane_bubble_wait_fallback"), "")])
    else:
        step = watch_scene.STEPS.get(phase)
        rows.append([(t(f"lane_bubble_phase_{step}" if step else "lane_bubble_phase_working"), "")])
    age = max(0.0, (now - snapshot.captured_at).total_seconds())
    # The step's own start; a run that recorded no step yet is in its first step since it began.
    started = (run.get("phase_started_at") or (run.get("started_at") if phase is None else None)) if run else None
    seconds = _age(started, snapshot.captured_at)
    if seconds is None and number == row.get("issue") and row.get("elapsed_seconds") is not None:
        seconds = row["elapsed_seconds"]
    if seconds is not None:
        rows.append([(t("lane_bubble_elapsed", elapsed=elapsed_text(seconds + age, language)), "")])
    if run:
        warning = watch_scene.activity_warning(snapshot, config, row, item, now)
        if warning:
            rows.append([(warning, "yellow")])
    return rows

