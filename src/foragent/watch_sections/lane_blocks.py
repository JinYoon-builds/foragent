"""The lane blocks (lane-view m2): one squirrel per lane, its acorns and its queue.

One block per lane under a heavy rule: working lanes, lanes with a
question, waiting lanes, and one shared block for every idle lane. A block shows the
lane's pixel squirrel (gnawing, waving, looking out or, shared, lying asleep), its name and
today's merges, its current acorns (number and one stage word), then its next work as one
'다음 N개 ▸ 누르면 목록' line. Issue explanations and the next-work list belong in the big
squirrel's bubble; lane paths are on the member card. All acorns remain scrollable.
Without pixels (ASCII, a non-UTF-8 terminal, animation off) or below 40 columns the
blocks are text only; every text fallback gives each acorn its own selectable row.
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
GAP = False  # The blocks follow the bubble, its bar and the roles directly.
# The lane blocks end the scene: an unreplaced section after them (the issue detail, the
# other installed gates) keeps its own blank-row boundary, as it had after the old lane
# rows. The pinned footer declares GAP = False, so it still follows directly.
GAP_AFTER = True

ART_CELLS = 11
CONTENT_FROM = 14  # Art, then three spaces.
ART_ROWS = 4
PIXELS_FROM = 40
# Hit = (x0, y0, x1, y1, target) in this block's own rows; Mark = (row, number).
Hit = tuple[int, int, int, int, Any]


def next_target(slug: str | None) -> tuple[str, tuple[str, str | None]]:
    """The click target of a lane's '다음 N개 ▸ 누르면 목록' line: the bubble lists that work."""
    return ("list", ("next", slug))


# Queued work that only waits its turn folds into the '다음 N개' count; a queued acorn whose
# class asks for someone (a question, a park, a wait) stays drawn beside the current ones.
FOLDED = ("next", "authored")


def split_next(upcoming: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(queued acorns still drawn, the next work the count line stands for)."""
    return ([item for item in upcoming if item["class"] not in FOLDED],
            [item for item in upcoming if item["class"] in FOLDED])


def _modes(config: Config, width: int) -> tuple[bool, bool, bool]:
    """(pixels, ascii, narrow) for this frame."""
    from foragent.watch_sections.lanes import glyph_mode

    ascii_glyphs = glyph_mode(config) == "ascii"
    narrow = width < PIXELS_FROM
    return config.animation and not ascii_glyphs and not narrow, ascii_glyphs, narrow


def _word(item: dict[str, Any], language: str) -> str:
    """The acorn's one stage word (Jin 2026-11-07): 구현/확인/리뷰/CI/차례, 질문 for the
    person's turn, 대기/막힘/멈춤 for the rest. It never names a person, the PM or a role,
    and never carries a detail such as the review round: whose turn it is and why are in the
    click bubble. Queued work says nothing."""
    from foragent import watch
    from foragent.messages.squirrel import PHASE_WORDS, phase_word

    kind, phase = item["class"], item["phase"]
    if kind == "question":
        return watch.text("lane_blocks_word_question", language)
    if kind == "parked":
        return watch.text("lane_blocks_word_parked", language)
    if item.get("state") == "blocked" and kind in {"working", "wait"}:
        return watch.text("lane_blocks_word_blocked", language)
    if kind == "wait":
        # The merge owner's queue is the issue's turn; every other wait only says it waits.
        return (phase_word(phase, language, short=True) if phase == "merge-owner-wait"
                else watch.text("lane_blocks_word_wait", language))
    if kind == "working":
        if phase is None or phase in PHASE_WORDS:
            return phase_word(phase or "baseline", language, short=True)
        return watch.text("lane_blocks_word_wait", language)  # A step no one named yet.
    return ""


def _acorn(item: dict[str, Any], language: str, tick: int, ascii_glyphs: bool, *, word: bool = True,
           selected: bool = False) -> Line:
    """The item's sideways acorn; ``word=False`` gives the ID-only acorn kept when the stage word
    does not fit (blocks.11: the complete issue ID first, the word only when it fits)."""
    from foragent.watch_art import sideways_acorn

    style = {"working": "working", "question": "question", "parked": "parked"}.get(item["class"], "next")
    return sideways_acorn(item["number"], _word(item, language) if word else "", style, tick=tick,
                          ascii=ascii_glyphs, selected=selected)


CURRENT_ROWS = 2  # Working acorns take at most this many rows in the pixel scene.


def _flow(units: list[tuple[Line, Line, str, int]], width: int, rows: int, prefix: Line, mark: str,
          per_row: int | None = None) -> tuple[list[Line], list[tuple[int, int, int, int]]]:
    """Acorns two spaces apart over at most ``rows`` rows, then …N for every one left out.

    ``units`` are (acorn spans, ID-only acorn spans, warning text or '', number); a
    continuation row is indented by the prefix. Every acorn, the first on a row too, keeps to
    the row's room, and the last row always keeps room for the omitted count. An acorn first
    on its row that does not fit drops its step word but keeps its complete ID; when even the
    ID does not fit beside the prefix, the prefix takes a row of its own and the acorns start
    at the left edge. Only an ID wider than the whole row is cut, with the mark. A warning
    that does not fit is cut, then dropped. Returns the rows and each placed acorn as
    (row, x0, x1, number).
    """
    from foragent import watch, watch_scene

    cells = lambda line: sum(watch._cells(value) for value, _ in line)
    indent = cells(prefix)
    lines: list[Line] = []
    if prefix and any(cells(compact) > width - indent for _, compact, _, _ in units):
        lines, prefix, indent, rows = [list(prefix)], [], 0, rows + 1
    room = max(1, width - indent)
    placed: list[tuple[int, int, int, int]] = []
    index, total = 0, len(units)
    while index < total and len(lines) < rows:
        last = len(lines) == rows - 1
        line: Line = list(prefix) if not lines else ([(" " * indent, "")] if indent else [])
        used = count = 0
        while index < total and not (per_row and count >= per_row):
            acorn, compact, warning, number = units[index]
            gap = 2 if count else 0
            rest = total - index - 1
            # The last row keeps room for the count of what follows.
            reserve = 2 + watch._cells(f"{mark}{rest}") if last and rest else 0
            free = room - used - gap - reserve
            size = cells(acorn)
            if size > free:
                if count:
                    break  # Next row, where it is first.
                if cells(compact) <= free:
                    acorn, size = compact, cells(compact)  # The complete ID stays; the step word goes.
                elif last and rest:
                    break  # Counted in …N.
                else:
                    acorn = watch_scene.fit_marked(compact, max(1, free), mark)  # Only a row narrower than the ID.
                    size = cells(acorn)
            extra: Line = []
            if warning:
                text = " " + warning
                spare = free - size
                if watch._cells(text) <= spare:
                    extra = [(text, "yellow")]
                elif count:
                    break  # The whole unit moves to the next row, where it is first.
                elif spare > watch._cells(mark) + 1:
                    extra = watch_scene.fit_marked([(text, "yellow")], spare, mark)
            if gap:
                line.append(("  ", ""))
            placed.append((len(lines), indent + used + gap, indent + used + gap + size, number))
            line += acorn + extra
            used += gap + size + cells(extra)
            count += 1
            index += 1
        lines.append(line)
    if index < total:
        lines[-1] += ([("  ", "")] if count else []) + [(f"{mark}{total - index}", "dim")]
    return lines, placed


def _name_row(snapshot: Snapshot, config: Config, row: dict[str, Any], width: int,
              mark: str) -> tuple[Line, int]:
    """The lane name and today's merges at the right; its paths are on the member card.

    The count goes before the name is cut. Returns the row and the name's cells.
    """
    from foragent import watch, watch_scene

    slug, language = row.get("slug"), config.language
    name = watch._clean(watch_scene.lane_name(row, config))
    count = watch_scene.today_count(snapshot, slug)
    right = ("" if not count else watch.text("lane_blocks_today_one", language) if count == 1
             else watch.text("lane_blocks_today", language, count=count))
    name_cells = watch._cells(name)
    if right and name_cells + 2 + watch._cells(right) > width:
        right = ""
    line = watch_scene.fit_marked([(name, "bold")], width, mark)
    if right:
        used = sum(watch._cells(value) for value, _ in line)
        line += [(" " * max(2, width - used - watch._cells(right)), ""), (right, "green")]
    return line, min(name_cells, width)


def _block(snapshot: Snapshot, config: Config, row: dict[str, Any], current: list[dict[str, Any]],
           upcoming: list[dict[str, Any]], width: int, tick: int, now: datetime, modes: tuple[bool, bool, bool],
           selected: int | None = None, chosen: int | None = None,
           ) -> tuple[list[Line], list[Hit], list[tuple[int, int]], int]:
    """One lane's content rows, their acorn hits and issue marks, and the name's cells."""
    from foragent import watch, watch_scene

    pixels, ascii_glyphs, _narrow = modes
    language = config.language
    mark = "..." if ascii_glyphs else "…"
    acorn_tick = tick if config.animation else 0
    # Text fallback keeps every issue selectable; pixel acorns fill the available width.
    per_row = None if pixels else 1
    name, name_cells = _name_row(snapshot, config, row, width, mark)
    lines: list[Line] = [name]
    hits: list[Hit] = []
    marks: list[tuple[int, int]] = []

    def units(items: list[dict[str, Any]]) -> list[tuple[Line, Line, str, int]]:
        return [(_acorn(item, language, acorn_tick, ascii_glyphs, selected=item["number"] == selected),
                 _acorn(item, language, acorn_tick, ascii_glyphs, word=False, selected=item["number"] == selected),
                 "", item["number"]) for item in items]

    def place(flowed: tuple[list[Line], list[tuple[int, int, int, int]]]) -> None:
        rows, placed = flowed
        start = len(lines)
        lines.extend(rows)
        for offset, x0, x1, number in placed:
            hits.append((x0, start + offset, min(width, x1), start + offset + 1, ("acorn", number)))
            # Every acorn is a keyboard stop, side by side ones too: Enter explains the chosen one.
            marks.append((start + offset, number))

    def group(items: list[dict[str, Any]], prefix: Line, rows: int) -> None:
        place(_flow(units(items), width, max(1, rows), prefix, mark, per_row))

    shown, upcoming = split_next(upcoming)
    current = current + shown
    if current:
        # The pixel scene keeps working acorns within two rows and counts the rest exactly.
        group(current, [], CURRENT_ROWS if pixels else len(current))
    if upcoming:
        # The next work is one count line, not acorns; a click lists it in the bubble.
        lines.append([])
        spans: Line = [(watch_scene.text("lane_blocks_next_count", language, ascii=ascii_glyphs,
                                         count=len(upcoming)), "dim")]
        numbers = [item["number"] for item in upcoming]
        if chosen in numbers:
            # Up/Down chose a folded issue: the count line names it, Enter explains it.
            spans.append(((" | " if ascii_glyphs else " · ") + f"#{chosen}", "bold"))
        count = watch_scene.fit_marked(spans, width, mark)
        lines.append(count)
        hits.append((0, len(lines) - 1, min(width, sum(watch._cells(value) for value, _ in count)), len(lines),
                     next_target(row.get("slug"))))
        # Every folded issue keeps a keyboard stop on its count line, so its details stay
        # reachable without a mouse.
        marks.extend((len(lines) - 1, number) for number in numbers)
    return lines, hits, marks, name_cells


def _idle_block(snapshot: Snapshot, config: Config, rows: list[dict[str, Any]], width: int,
                modes: tuple[bool, bool, bool]) -> tuple[list[Line], list[Hit]]:
    """The shared block of idle lanes: '쉬고 있어요', then every name with ✓N and 💭."""
    from foragent import watch, watch_scene
    from foragent.watch_dream import GLYPHS as DREAM

    pixels, ascii_glyphs, _ = modes
    language = config.language
    lines: list[Line] = ([[]] if pixels else []) + [[(watch.text("lane_header_resting", language), "dim")]]
    hits: list[Hit] = []
    entries: list[tuple[Line, int, str]] = []
    for row in rows:
        slug = row.get("slug")
        spans: Line = [(watch._clean(watch_scene.lane_name(row, config)), "")]
        count = watch_scene.today_count(snapshot, slug)
        if count:
            spans.append((f" {'v' if ascii_glyphs else '✓'}{count}", "green"))
        if row.get("dreaming"):
            spans.append((" " + DREAM["ascii" if ascii_glyphs else "emoji"], ""))
        # An entry wider than the row keeps the mark that shows it was cut.
        spans = watch_scene.fit_marked(spans, width, "..." if ascii_glyphs else "…")
        entries.append((spans, sum(watch._cells(value) for value, _ in spans), slug))
    line: Line = []
    used = 0
    for spans, cells, slug in entries:
        if line and used + 3 + cells > width:
            lines.append(line)
            line, used = [], 0
        if line:
            line.append((" | " if ascii_glyphs else " · ", "dim"))
            used += 3
        hits.append((used, len(lines), min(width, used + cells), len(lines) + 1, ("squirrel", slug)))
        line += spans
        used += cells
    if line:
        lines.append(line)
    return lines, hits


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch, watch_scene
    from foragent.watch_art import LANE_POSES, lookout, lying_asleep, pixel_rows

    width, tick, now = max(1, context["width"]), context.get("tick", 0), context["now"]
    top, hits, marks = context.get("top", 0), context.get("hits", []), context.get("marks")
    modes = _modes(config, width)
    pixels, ascii_glyphs, _ = modes
    # The acorn whose explanation the bubble shows is drawn selected (brighter, barred).
    target = (context.get("state") or {}).get("explanation_target")
    selected = target[1] if isinstance(target, tuple) and len(target) == 2 and target[0] == "acorn" else None
    chosen = (context.get("state") or {}).get("selected")  # The issue Up/Down (or a click) chose.
    left = CONTENT_FROM if pixels else 0
    inside = max(1, width - left)
    rule = (("-" if ascii_glyphs else "━") * width, "separator")
    out: list[Line] = []
    needs = watch_scene.question_numbers(snapshot)
    lanes = []
    for row in watch_scene.lane_rows(snapshot.status):
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
        content, own, rows, name_cells = _block(snapshot, config, row, current, upcoming, inside, tick, now, modes,
                                               selected, chosen)
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
