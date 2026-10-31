"""The lane blocks (lane-view m2): one squirrel per lane, its acorns and its queue.

First a compact evidence row (the orchestrator is off, planning requests, the inbox
count), then one block per lane under a heavy rule: working lanes, lanes with a
question, waiting lanes, and one shared block for every idle lane. A block shows the
lane's pixel squirrel (gnawing, waving, looking out or, shared, lying asleep), its name,
owned paths and today's merges, its current acorns, a waiting reason, then '다음' with
the queue. Acorns flow over two rows at most and end in an exact …N for the rest.
Without pixels (ASCII, a non-UTF-8 terminal, animation off) or below 40 columns the
blocks are text only; below 40 every acorn has its own row.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 12
REPLACES = ("foragent.watch_sections.lanes", "foragent.watch_sections.acorns",
            "foragent.watch_sections.done", "foragent.watch_sections.finished")
GAP = False
GAP_AFTER = False

ART_CELLS = 11
CONTENT_FROM = 14  # Art, then three spaces.
ART_ROWS = 4
PIXELS_FROM = 40
ACORN_ROWS = 2  # Current acorns and queued ones each take at most this many rows.
WORDS = {"question": "lane_blocks_word_question", "parked": "lane_blocks_word_parked",
         "wait": "lane_blocks_word_wait"}
# Hit = (x0, y0, x1, y1, target) in this block's own rows; Mark = (row, number).
Hit = tuple[int, int, int, int, Any]


def _modes(config: Config, width: int) -> tuple[bool, bool, bool]:
    """(pixels, ascii, narrow) for this frame."""
    from foragent.watch_sections.lanes import glyph_mode

    ascii_glyphs = glyph_mode(config) == "ascii"
    narrow = width < PIXELS_FROM
    return config.animation and not ascii_glyphs and not narrow, ascii_glyphs, narrow


def _cut(value: str, width: int, mark: str) -> str:
    from foragent import watch

    if watch._cells(value) <= width:
        return value
    room = max(0, width - watch._cells(mark))
    return "".join(part for part, _ in watch._fit([(value, "")], room)).rstrip() + mark


def _acorn(item: dict[str, Any], language: str, tick: int, ascii_glyphs: bool) -> Line:
    from foragent import watch, watch_scene
    from foragent.watch_art import sideways_acorn

    kind = item["class"]
    if kind == "working":
        step = watch_scene.STEPS.get(item["phase"])
        word = watch.text(f"lane_blocks_step_{step}", language) if step else ""
    else:
        word = watch.text(WORDS[kind], language) if kind in WORDS else ""
    style = {"working": "working", "question": "question", "parked": "parked"}.get(kind, "next")
    return sideways_acorn(item["number"], word, style, tick=tick, ascii=ascii_glyphs)


def _flow(units: list[tuple[Line, int, int, int]], width: int, rows: int, prefix: Line, mark: str,
          per_row: int | None = None) -> tuple[list[Line], list[tuple[int, int, int, int]]]:
    """Acorns two spaces apart over at most ``rows`` rows, then …N for every one left out.

    ``units`` are (spans, acorn cells, all cells, number); a continuation row is indented
    by the prefix. Returns the rows and each placed acorn as (row, x0, x1, number).
    """
    from foragent import watch

    indent = sum(watch._cells(value) for value, _ in prefix)
    room = max(1, width - indent)
    lines: list[Line] = []
    placed: list[tuple[int, int, int, int]] = []
    index, total = 0, len(units)
    while index < total and len(lines) < rows:
        last = len(lines) == rows - 1
        line: Line = list(prefix) if not lines else ([(" " * indent, "")] if indent else [])
        used = count = 0
        while index < total:
            spans, acorn_cells, cells, number = units[index]
            gap = 2 if count else 0
            rest = total - index - 1
            # The last row keeps room for the count of what follows.
            reserve = 2 + watch._cells(f"{mark}{rest}") if last and rest else 0
            if count and (used + gap + cells + reserve > room or (per_row and count >= per_row)):
                break
            if gap:
                line.append(("  ", ""))
            placed.append((len(lines), indent + used + gap, indent + used + gap + acorn_cells, number))
            line += spans
            used += gap + cells
            count += 1
            index += 1
        lines.append(line)
    if index < total:
        lines[-1] += [("  ", ""), (f"{mark}{total - index}", "dim")]
    return lines, placed


def _name_row(snapshot: Snapshot, config: Config, row: dict[str, Any], width: int,
              mark: str) -> tuple[Line, int]:
    """The lane name, two spaces and its owned paths, and today's merges at the right.

    The paths are cut first; the count goes before the name is cut. Returns the row and
    the name's cells.
    """
    from foragent import watch

    slug, language = row.get("slug"), config.language
    name = watch._clean(row.get("lane_display") or slug or "")
    facts = snapshot.lane_facts.get(slug)
    paths = facts.get("owned_paths") if isinstance(facts, dict) else (config.lanes.get(slug) or {}).get("paths")
    joined = ", ".join(str(path) for path in paths or ())
    count = (snapshot.today_done or {}).get(slug, 0) if snapshot.today_done is not None else 0
    right = ("" if not count else watch.text("lane_blocks_today_one", language) if count == 1
             else watch.text("lane_blocks_today", language, count=count))
    name_cells = watch._cells(name)
    if right and name_cells + 2 + watch._cells(right) > width:
        right = ""
    room = width - name_cells - (2 + watch._cells(right) if right else 0) - 2
    line: Line = [(name, "bold")]
    if joined and room > watch._cells(mark):
        line += [("  ", ""), (_cut(joined, room, mark), "dim")]
    if right:
        used = sum(watch._cells(value) for value, _ in line)
        line += [(" " * max(2, width - used - watch._cells(right)), ""), (right, "green")]
    return line, min(name_cells, width)


def _block(snapshot: Snapshot, config: Config, row: dict[str, Any], current: list[dict[str, Any]],
           upcoming: list[dict[str, Any]], width: int, tick: int, now: datetime, modes: tuple[bool, bool, bool],
           ) -> tuple[list[Line], list[Hit], list[tuple[int, int]], int]:
    """One lane's content rows, their acorn hits and issue marks, and the name's cells."""
    from foragent import watch, watch_scene

    pixels, ascii_glyphs, narrow = modes
    language = config.language
    mark = "..." if ascii_glyphs else "…"
    acorn_tick = tick if config.animation else 0
    per_row = 1 if narrow else None
    name, name_cells = _name_row(snapshot, config, row, width, mark)
    lines: list[Line] = [name]
    hits: list[Hit] = []
    marks: list[tuple[int, int]] = []

    def units(items: list[dict[str, Any]], warnings: bool) -> list[tuple[Line, int, int, int]]:
        found = []
        for item in items:
            spans = _acorn(item, language, acorn_tick, ascii_glyphs)
            acorn_cells = sum(watch._cells(value) for value, _ in spans)
            # A worker warning stands right beside its acorn (blocks.08).
            warning = watch_scene.activity_warning(snapshot, config, row, item, now) if warnings else ""
            if warning:
                spans = [*spans, (" " + warning, "yellow")]
            found.append((spans, acorn_cells, sum(watch._cells(value) for value, _ in spans), item["number"]))
        return found

    def place(flowed: tuple[list[Line], list[tuple[int, int, int, int]]]) -> None:
        rows, placed = flowed
        start = len(lines)
        lines.extend(rows)
        firsts: set[int] = set()
        for offset, x0, x1, number in placed:
            hits.append((x0, start + offset, min(width, x1), start + offset + 1, ("acorn", number)))
            if offset not in firsts:
                firsts.add(offset)
                marks.append((start + offset, number))

    if current:
        place(_flow(units(current, True), width, ACORN_ROWS, [], mark, per_row))
    waiting = watch_scene.representative_wait(row, current, upcoming)
    if waiting is not None:
        lines.append([(_cut(watch_scene.waiting_reason(snapshot, row, waiting, language), width, mark), "dim")])
    if upcoming:
        lines.append([])
        prefix = [(watch.text("lane_blocks_next", language), "dim")]
        place(_flow(units(upcoming, False), width, ACORN_ROWS, prefix, mark, per_row))
    return lines, hits, marks, name_cells


def _idle_block(snapshot: Snapshot, config: Config, rows: list[dict[str, Any]], width: int,
                modes: tuple[bool, bool, bool]) -> tuple[list[Line], list[Hit]]:
    """The shared block of idle lanes: '쉬고 있어요', then every name with ✓N and 💭."""
    from foragent import watch
    from foragent.watch_dream import GLYPHS as DREAM

    pixels, ascii_glyphs, _ = modes
    language = config.language
    lines: list[Line] = ([[]] if pixels else []) + [[(watch.text("lane_header_resting", language), "dim")]]
    hits: list[Hit] = []
    entries: list[tuple[Line, int, str]] = []
    for row in rows:
        slug = row.get("slug")
        spans: Line = [(watch._clean(row.get("lane_display") or slug or ""), "")]
        count = (snapshot.today_done or {}).get(slug, 0) if snapshot.today_done is not None else 0
        if count:
            spans.append((f" {'v' if ascii_glyphs else '✓'}{count}", "green"))
        if row.get("dreaming"):
            spans.append((" " + DREAM["ascii" if ascii_glyphs else "emoji"], ""))
        entries.append((spans, sum(watch._cells(value) for value, _ in spans), slug))
    line: Line = []
    used = 0
    for spans, cells, slug in entries:
        if line and used + 3 + cells > width:
            lines.append(line)
            line, used = [], 0
        if line:
            line.append((" · ", "dim"))
            used += 3
        hits.append((used, len(lines), min(width, used + cells), len(lines) + 1, ("squirrel", slug)))
        line += spans
        used += cells
    if line:
        lines.append(line)
    return lines, hits


def _evidence(snapshot: Snapshot, config: Config, now: datetime) -> Line:
    """The orchestrator's missing watch, the planning requests (D76) and the inbox count, on one row."""
    from foragent import watch
    from foragent.watch_sections import orch_panel

    status, language = snapshot.status, config.language
    t = lambda key, **values: watch.text(key, language, **values)
    age = max(0.0, (now - snapshot.captured_at).total_seconds())
    parts: list[tuple[str, str]] = []
    heartbeat = snapshot.heartbeats.get("orch")
    fresh = heartbeat is not None and heartbeat + age <= orch_panel.ORCH_FRESH_SECONDS
    if not fresh and (heartbeat is not None or status.get("active_runs")
                      or any(watch._has_work(row) for row in status.get("lanes", {}).values())):
        parts.append((t("watch_orch_off"), "yellow"))
    if snapshot.requests:
        count = lambda name: sum(row.get("status") == name for row in snapshot.requests)
        parts.append((t("watch_requests", processing=count("processing"), done=count("done")), ""))
    if snapshot.panel.get("inbox") is not None:
        parts.append((t("orch_panel_for_you", count=snapshot.panel["inbox"]), ""))
    line: Line = []
    for index, (value, style) in enumerate(parts):
        line += ([(" · ", "dim")] if index else []) + [(value, style)]
    return line


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch, watch_scene
    from foragent.watch_art import LANE_POSES, lookout, lying_asleep, pixel_rows

    width, tick, now = max(1, context["width"]), context.get("tick", 0), context["now"]
    top, hits, marks = context.get("top", 0), context.get("hits", []), context.get("marks")
    modes = _modes(config, width)
    pixels, ascii_glyphs, _ = modes
    left = CONTENT_FROM if pixels else 0
    inside = max(1, width - left)
    rule = (("-" if ascii_glyphs else "━") * width, "separator")
    out: list[Line] = []
    evidence = _evidence(snapshot, config, now)
    if evidence:
        out.append(watch._fit(evidence, width))
    needs = watch_scene.questions(snapshot.status)
    lanes = []
    for row in snapshot.status.get("lanes", {}).values():
        current, upcoming = watch_scene.lane_items(snapshot, row, needs)
        lanes.append((watch_scene.CATEGORIES.index(watch_scene.category(current, upcoming)), row, current, upcoming))
    lanes.sort(key=lambda lane: lane[0])  # Stable: lanes of one kind keep their order.
    poses = {0: lambda: LANE_POSES["gnaw" if (tick // 2) % 2 == 0 else "sit"],
             1: lambda: LANE_POSES["wave" if (tick // 2) % 2 == 0 else "sit"],
             2: lambda: lookout(tick), 3: lambda: lying_asleep(tick // 2)}

    def add(content: list[Line], own: list[Hit], kind: int, slug: str | None) -> None:
        """Place one block under its rule: the squirrel left, the content from cell 14."""
        out.append([rule])
        start = top + len(out)
        size = max(ART_ROWS, len(content)) if pixels else len(content)
        art: list[Line] = []
        if pixels:
            art = pixel_rows(poses[kind]())
            offset = max(0, (size - ART_ROWS) // 2)
            art = [[]] * offset + art
            if slug is not None:
                hits.append((0, start + offset, ART_CELLS, start + offset + ART_ROWS, ("squirrel", slug)))
        for index in range(size):
            line = content[index] if index < len(content) else []
            if pixels:
                piece = art[index] if index < len(art) else []
                line = [*watch._fit(piece, ART_CELLS), ("   ", ""), *watch._fit(line, inside)]
            out.append(watch._fit(line, width))
        hits.extend((x0 + left, y0 + start, x1 + left, y1 + start, target) for x0, y0, x1, y1, target in own)

    idle = [row for kind, row, *_ in lanes if kind == 3]
    for kind, row, current, upcoming in lanes:
        if kind == 3:
            continue
        content, own, rows, name_cells = _block(snapshot, config, row, current, upcoming, inside, tick, now, modes)
        if not pixels:
            own.append((0, 0, name_cells, 1, ("squirrel", row.get("slug"))))  # The name is the squirrel.
        first = top + len(out) + 1
        add(content, own, kind, row.get("slug"))
        if marks is not None:
            marks.extend((first + y - top, number, row.get("slug")) for y, number in rows)
    if idle:
        content, own = _idle_block(snapshot, config, idle, inside, modes)
        add(content, own, 3, None)
    return out
