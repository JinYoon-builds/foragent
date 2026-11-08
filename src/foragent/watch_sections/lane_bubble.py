"""The big orchestrator squirrel, its speech bubble and the r57 progress bar (lane-view m2).

By default the bubble says only what the pipeline and the header do not: the finish
estimate with the recent merge rate, how many planning-session requests are open (a click
lists them, never their text by default) and today's newest merges, in a bubble as tall as
its lines. Otherwise it explains the selected target through watch_explanations; acorns
and the request list are explained here, an acorn's GitHub link line among six rows. From 80 columns
the sprite stands left of the bubble with its mouth on row 3; narrower, it stands
above the full-width bubble. Without pixels (ASCII, a non-UTF-8 terminal or animation
off), the bubble takes the whole width.
The bar under the bubble counts today's merges against them plus the approved work left.
"""

import re
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


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def latest_merges(snapshot: Snapshot) -> list[int]:
    """The four newest distinct merges from local midnight through the capture time, newest first.

    Ties keep the map's order; a hand-built map obeys the same window as a collected one.
    """
    if snapshot.now_view is not None:
        return snapshot.now_view.get("summary", {}).get("merges", [])
    captured = _aware(snapshot.captured_at)
    midnight = captured.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    timed = [(number, _aware(moment)) for number, moment in (snapshot.merged_at or {}).items()
             if isinstance(moment, datetime) and midnight <= _aware(moment) <= captured]
    timed.sort(key=lambda pair: pair[1], reverse=True)  # Stable: equal times keep their order.
    return [number for number, _ in timed[:MERGES_SHOWN]]


def bar_counts(snapshot: Snapshot) -> tuple[int, int]:
    """(done, total): today's merges, and those plus the distinct approved issues left."""
    if snapshot.now_view is not None:
        summary = snapshot.now_view.get("summary", {})
        return summary.get("today", 0), summary.get("total", 0)
    issues = snapshot.status.get("issues")
    left = {item.get("number") for name in REMAINING
            for item in ((issues or {}).get(name) or () if isinstance(issues, dict) else ())
            if isinstance(item, dict) and item.get("number") is not None}
    done = max(0, int(snapshot.today or 0))
    return done, done + len(left)


def _bar(snapshot: Snapshot, language: str, width: int, mark: str = "…", *, ascii: bool = False) -> list[Line]:
    from foragent import watch, watch_scene

    done, total = bar_counts(snapshot)
    if not total:
        return []
    percent = round(100 * done / total)
    label = (watch_scene.text("lane_bubble_bar_one", language, ascii=ascii, percent=percent, done=done) if total == 1
             else watch_scene.text("lane_bubble_bar", language, ascii=ascii, percent=percent, done=done, total=total))
    cells = max(0, min(BAR_CELLS, width - watch._cells(label) - 2))
    filled = round(cells * done / total)
    if not cells:
        return [watch_scene.fit_marked([(label, "")], width, mark)]
    ink, empty = ("#", "-") if ascii else ("█", "░")
    return [[(ink * filled, "green"), (empty * (cells - filled), "dim"), ("  ", ""), (label, "")]]


def _clock(at: datetime, now: datetime, language: str) -> str:
    """HH:MM, with the existing date words (progress._when) when it falls on a later day."""
    from foragent.watch_sections import progress

    local = at.astimezone()
    return progress._when(at, now, language, f"{local.hour:02}:{local.minute:02}")


def orchestrator_off(snapshot: Snapshot, now: datetime) -> bool:
    """No fresh orchestrator watch while one ran before or work is on the lanes."""
    from foragent import watch, watch_scene
    from foragent.watch_sections import orch_panel

    if snapshot.now_view is not None:
        return bool(snapshot.now_view.get("summary", {}).get("orch_off"))
    age = max(0.0, (now - snapshot.captured_at).total_seconds())
    heartbeat = (snapshot.heartbeats or {}).get("orch")
    if heartbeat is not None and heartbeat + age <= orch_panel.ORCH_FRESH_SECONDS:
        return False
    return bool(heartbeat is not None or snapshot.status.get("active_runs")
                or any(watch._has_work(row) for row in watch_scene.lane_rows(snapshot.status)))


# A role marks a proposal's decision owner on its first line (orch_role_prompt), e.g.
# "Decision: orchestrator"; that header routes the request and is not its subject.
ROUTING_HEADER = re.compile(r"Decision:\s*\S+")


def request_subject(text: str) -> str:
    """The request's first content line, skipping a routing header line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and ROUTING_HEADER.fullmatch(lines[0]):
        lines = lines[1:]
    return lines[0] if lines else ""


def processing_request(snapshot: Snapshot) -> str | None:
    """The subject of the newest planning-session request being handled now (D76).

    Requests from roles (the architect's proposals for the orchestrator) are not the
    planning session's and never take this line.
    """
    if snapshot.now_view is not None:
        return None  # The main scene shows planning requests only as a count (planning_requests).
    processing = [row for row in snapshot.requests or ()
                  if isinstance(row, dict) and row.get("status") == "processing"
                  and row.get("from") == "planning" and not row.get("role")]
    if not processing:
        return None
    return request_subject(processing[-1].get("text") or "")


OPEN_REQUEST = ("new", "processing")


def open_requests(rows: list[dict] | None) -> list[dict]:
    """The planning session's requests still open, oldest first; role proposals are not its own."""
    return [row for row in rows or () if isinstance(row, dict) and row.get("status") in OPEN_REQUEST
            and row.get("from") == "planning" and not row.get("role")]


def planning_requests(snapshot: Snapshot) -> int:
    """How many planning-session requests are open; the bubble never shows their text."""
    if snapshot.now_view is not None:
        value = snapshot.now_view.get("summary", {}).get("requests")
        return value if type(value) is int and value > 0 else 0
    return len(open_requests(snapshot.requests))


def work_summary(issues: int, lanes: int, language: str) -> str:
    from foragent import watch

    key = ("lane_bubble_idle" if not issues else "lane_bubble_working_one_each" if issues == 1 and lanes == 1
           else "lane_bubble_working_one_issue" if issues == 1
           else "lane_bubble_working_one_lane" if lanes == 1 else "lane_bubble_working")
    return watch.text(key, language, issues=issues, lanes=lanes)


def summary(snapshot: Snapshot, config: Config, now: datetime) -> list[tuple[str, str, str]]:
    """The default sentences in order, as (role, text, style).

    Only what the pipeline line and the header do not already say: the finish estimate with
    the recent merge rate, the open planning-session requests as a count (a click lists
    them) and the latest merges. A missing gate snapshot or orchestrator is said first.
    """
    from foragent import watch, watch_scene
    from foragent.watch_sections.lanes import glyph_mode

    language, ascii = config.language, glyph_mode(config) == "ascii"
    t = lambda key, **values: watch_scene.text(key, language, ascii=ascii, **values)
    sentences: list[tuple[str, str, str]] = []
    if snapshot.now_view is not None and snapshot.now_view.get("pending"):
        sentences.append(("pending", t("lane_bubble_snapshot_pending"), ""))
    if orchestrator_off(snapshot, now):
        warning = t("watch_orch_off")
        sentences.append(("orchestrator", watch_scene.generated_ascii(warning) if ascii else warning, "yellow"))
    if snapshot.eta is not None:
        low, high = snapshot.eta_range or (snapshot.eta, snapshot.eta)
        low, high = _clock(low, now, language), _clock(high, now, language)
        sentences.append(("eta", t("lane_bubble_eta", low=low) if low == high
                          else t("lane_bubble_eta_range", low=low, high=high), ""))
    recent = snapshot.recent or 0
    if recent > 0:
        sentences.append(("pace", t("lane_bubble_pace_one") if recent == 1
                          else t("lane_bubble_pace", count=recent), ""))
    requests = planning_requests(snapshot)
    if requests:
        sentences.append(("request", t("lane_bubble_requests_one") if requests == 1
                          else t("lane_bubble_requests", count=requests), "bold"))
    merged = latest_merges(snapshot)
    if merged:
        sentences.append(("merges", t("lane_bubble_merges", ids=", ".join(f"#{n}" for n in merged)), ""))
    return sentences


def _summary_rows(sentences: list[tuple[str, str, str]], width: int, mark: str) -> tuple[list[Line], int | None]:
    """Wrap each sentence on its own rows (the pace follows the estimate), within six rows,
    and the row where the request count starts (None when it is not shown).

    On overflow the request count goes first, then the pace; what still does not fit is cut
    after the sixth row, which ends in an ellipsis.
    """
    from foragent import watch

    def wrapped(kept: list[tuple[str, str, str]]) -> tuple[list[Line], int | None]:
        rows: list[Line] = []
        request = None
        for index, (role, value, style) in enumerate(kept):
            if role == "pace" and index and kept[index - 1][0] == "eta":
                continue
            if role == "eta" and index + 1 < len(kept) and kept[index + 1][0] == "pace":
                value += " " + kept[index + 1][1]
            if role == "request":
                request = len(rows)
            rows += watch._wrap(value, width, style)
        return rows, request

    kept = list(sentences)
    rows, request = wrapped(kept)
    for role in ("request", "pace"):
        if len(rows) <= BODY_ROWS:
            break
        kept = [item for item in kept if item[0] != role]
        rows, request = wrapped(kept)
    return _clip(rows, width, mark), request if request is not None and request < BODY_ROWS else None


def _clip(rows: list[Line], width: int, mark: str, limit: int = BODY_ROWS) -> list[Line]:
    """At most ``limit`` rows; when any were left out the last kept row ends in the mark."""
    if len(rows) <= limit:
        return rows
    if limit <= 0:
        return []
    from foragent.watch_scene import fit_marked

    return rows[:limit - 1] + [fit_marked(rows[limit - 1], width, mark, omitted=True)]


def _explanation_rows(found: list[Line], width: int, mark: str) -> list[Line]:
    """Each logical row of an explanation wrapped by cells, within six rows.

    A row may wrap only into rows no later logical row needs: every logical row keeps at
    least one row, so long metadata (a lane with many paths, a long lane name) is cut with
    the mark instead of pushing out the step, its time or a warning. The acorn explainer
    already keeps at most six logical rows (``keep_rows``), so the bubble never grows.
    """
    from foragent import watch

    limit = BODY_ROWS
    rows: list[Line] = []
    for index, line in enumerate(found):
        line = [(watch._clean(value) if any(watch.unicodedata.category(char).startswith("C") for char in value)
                 else value, style) for value, style in line]
        text = "".join(value for value, _ in line)
        style = next((style for value, style in line if value.strip()), "")
        wrapped = (watch._wrap(text, width, style) if watch._cells(text) > width else [line]) if text.strip() else [[]]
        later = len(found) - index - 1
        rows += _clip(wrapped, width, mark, max(1, limit - len(rows) - later))
    return _clip(rows, width, mark, limit)


# Box glyphs: corners (top left, top right, bottom left, bottom right), edge, side and mouth.
BOX = {"emoji": ("╭╮╰╯", "─", "│", "┤"), "ascii": ("++++", "-", "|", "|")}


def _box(body: list[Line], width: int, mouth: bool, glyphs: str, height: int | None = BODY_ROWS) -> list[Line]:
    """The bordered bubble with ``height`` body rows; None fits its lines (the mouth row at least)."""
    from foragent import watch

    corners, edge, side, opening = BOX[glyphs]
    inside = max(0, width - 4)
    rows = [[(corners[0] + edge * max(0, width - 2) + corners[1], "edge")]]
    count = height if height is not None else max(len(body), MOUTH_ROW if mouth else 1)
    for index in range(count):
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
    from foragent.watch_sections.lanes import glyph_mode

    ascii = glyph_mode(config) == "ascii"
    sprite = _pixels(config)
    beside = sprite and width >= SPRITE_FROM
    box_width = width - SPRITE_CELLS - SPRITE_GAP if beside else width
    inside = max(1, box_width - 4)
    target = state.get("explanation_target") if state is not None else None
    found = (watch_explanations.explain(target, snapshot, config, repo_root, now=now, width=inside, state=state)
             if target is not None else None)
    request = None
    if found is not None:
        body = _explanation_rows(found, inside, mark)
    else:
        body, request = _summary_rows(summary(snapshot, config, now), inside, mark)
    if width < 4:
        # Too narrow for borders: the sentences alone, cut to the width.
        from foragent import watch_scene

        box = [watch_scene.fit_marked(line, width, mark) for line in body if line]
        offset, left = 0, 0
        request = None if request is None else sum(1 for line in body[:request] if line)
    else:
        box = _box(body, box_width, beside, glyph_mode(config), BODY_ROWS if found is not None else None)
        offset, left = 1, 2
    art = pixel_rows(ORCHESTRATOR) if sprite else []
    if request is not None:
        # The count opens the list of requests in the bubble (explain: ("requests", None)).
        y = top + offset + request + (0 if beside else len(art))
        x = left + (SPRITE_CELLS + SPRITE_GAP if beside else 0)
        hits.append((x, y, min(width, x + inside), y + 1, ("requests", None)))
    if sprite:
        if beside:
            box = [[*pixels, (" " * (SPRITE_CELLS - len(pixels) + SPRITE_GAP), ""), *row]
                   for pixels, row in watch.zip_longest(art, box, fillvalue=[])]
        else:
            box = [*[watch._fit(row, width) for row in art], *box]
        hits.append((0, top, min(width, SPRITE_CELLS), top + len(art), ("orchestrator", None)))
    return box + _bar(snapshot, config.language, width, mark, ascii=ascii)


def _goal(state: dict | None, number: int, captured: object = None) -> str:
    """The first line of the issue's recorded Goal, or "" when no local record has one.

    Read once per selected issue and snapshot (like the other selection facts), not on every
    frame: the view repaints twice a second."""
    from foragent import watch
    from foragent.issue import parse_body

    store = (state or {}).get("store")
    if store is None:
        return ""
    key = (number, captured)
    cached = state.get("explanation_goal")
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        row = next((row for row in store.local_issues() if row.get("number") == number), None)
    except (OSError, ValueError):
        return ""
    goal = parse_body((row or {}).get("body") or "").goal
    found = watch._clean(goal.splitlines()[0]) if goal else ""
    state["explanation_goal"] = (key, found)
    return found


def _request_rows(snapshot: Snapshot, config: Config, width: int, state: dict | None) -> list[Line]:
    """The open planning-session requests, one subject per row, read from the local box once
    per snapshot (the now snapshot carries only their count)."""
    from foragent import orch_inbox, watch, watch_scene
    from foragent.watch_sections.lanes import glyph_mode

    language, mark = config.language, _ellipsis(config)
    t = lambda key, **values: watch_scene.text(key, language, ascii=glyph_mode(config) == "ascii", **values)
    found = open_requests(snapshot.requests)
    store = (state or {}).get("store")
    if snapshot.now_view is not None and store is not None:
        key = snapshot.captured_at
        cached = state.get("explanation_requests")
        if cached is None or cached[0] != key:
            try:
                cached = (key, open_requests(orch_inbox.requests(store)))
            except (OSError, ValueError):
                cached = (key, [])
            state["explanation_requests"] = cached
        found = cached[1]
    if not found:
        return [[(t("lane_bubble_requests_none"), "")]]
    rows: list[Line] = [[(t("lane_bubble_requests_title_one") if len(found) == 1
                          else t("lane_bubble_requests_title", count=len(found)), "bold")]]
    for row in found:
        subject = watch._clean(request_subject(str(row.get("text") or "")))
        rows.append(watch_scene.fit_marked([(f"#{row.get('id')} {subject}".rstrip(), "")], width, mark))
    return rows


# Which of an acorn's facts the eight-row bubble keeps when they do not all fit (lower stays
# longer). A worker warning shares the step's row, so it stays with the step.
KEEP = {"title": 0, "link": 0, "state": 1, "context": 2, "goal": 3, "lane": 4, "elapsed": 5,
        "detail": 6}


class Rows(list):
    """An acorn explanation's rows, each with how long it stays when the bubble is full."""

    def __init__(self, rows: list[Line], ranks: list[int]) -> None:
        super().__init__(rows)
        self.ranks = list(ranks)

    def add(self, line: Line, kind: str) -> None:
        self.append(line)
        self.ranks.append(KEEP[kind])

    def beside_state(self, separator: Line, spans: Line, width: int) -> None:
        """Put ``spans`` after the step's row when they fit there, else on a row of the same rank
        right below it: either way they stay as long as the step does."""
        from foragent import watch

        index = max((i for i, rank in enumerate(self.ranks) if rank == KEEP["state"]), default=None)
        if index is None:
            # No step row (an authored wait): the spans are its own step-ranked row.
            self.append(spans)
            self.ranks.append(KEEP["state"])
            return
        joined = [*self[index], *separator, *spans]
        if watch._cells("".join(value for value, _ in joined)) <= width:
            self[index] = joined
        else:
            self.insert(index + 1, spans)
            self.ranks.insert(index + 1, KEEP["state"])

    def keep_rows(self) -> list[Line]:
        """At most BODY_ROWS rows in their own order: the lowest-ranked go, the later first."""
        order = sorted(range(len(self)), key=lambda index: (self.ranks[index], index))
        kept = sorted(order[:BODY_ROWS])
        return [self[index] for index in kept]


def explain(target: tuple[str, int | str | None], snapshot: Snapshot, config: Config, repo_root: Path, *,
            now: datetime, width: int, state: dict | None) -> list[Line] | None:
    """An acorn's rows: #N and title (one row), its lane, what it is doing and for how long,
    and a worker warning when there is one. Authored waits show their recorded
    context here; it never appears under an acorn in the lane block.
    Other targets are not this section's."""
    from foragent import watch, watch_scene
    from foragent.status import _age
    from foragent.watch_sections.lanes import elapsed_text

    if target == ("requests", None):
        return _request_rows(snapshot, config, width, state)
    if not (isinstance(target, tuple) and len(target) == 2 and target[0] == "acorn" and type(target[1]) is int):
        return None
    number, language = target[1], config.language
    from foragent.watch_sections.lanes import glyph_mode

    ascii = glyph_mode(config) == "ascii"
    t = lambda key, **values: watch_scene.text(key, language, ascii=ascii, **values)
    needs = watch_scene.question_numbers(snapshot)
    for row in watch_scene.lane_rows(snapshot.status):
        current, upcoming = watch_scene.lane_items(snapshot, row, needs)
        item = next((item for item in current + upcoming if item["number"] == number), None)
        if item is not None:
            break
    else:
        return None  # Not an acorn on screen: the summary stays.
    slug, name = row.get("slug"), watch_scene.lane_name(row, config)
    paths = watch_scene.owned_paths(snapshot, config, slug)
    rows = Rows([watch_scene.fit_marked([(watch._clean(f"#{number} {item['title']}".rstrip()), "bold")],
                                        width, _ellipsis(config))], [KEEP["title"]])
    goal = _goal(state, number, snapshot.captured_at)
    if goal:
        rows.add(watch_scene.fit_marked([(goal, "")], width, _ellipsis(config)), "goal")
    declared = (snapshot.declared_files or {}).get(number)
    if declared is not None:
        # The issue's own Files are its scope; the lane's globs are not its evidence.
        rows.add([(t("lane_bubble_lane_files", lane=name, paths=", ".join(declared)) if declared
                   else t("lane_bubble_lane_no_files", lane=name), "")], "lane")
    else:
        rows.add([(t("lane_bubble_lane_paths", lane=name, paths=", ".join(paths)) if paths
                   else t("lane_bubble_lane", lane=name), "")], "lane")
    kind, run, phase = item["class"], item["run"], item["phase"]

    def with_context(rows):
        if item.get("authored"):
            from foragent.watch_waiting import explanation

            # The recorded wait adds to what the acorn's own facts say; it never replaces
            # the step, its time or a stalled or missing worker's warning (see KEEP).
            for line in explanation(item["authored"], language, width, mark=_ellipsis(config)):
                rows.add(line, "context")
        # What the collected facts say it waits for (issues, shared files, slots, the machine):
        # every class gets it, a worker in a wait phase and an authored wait as much as the next one.
        # It says why the step waits, so it stays beside the step as long as the step does.
        reason = watch_scene.waiting_reason(snapshot, row, number, language, ascii=ascii)
        if reason != t("lane_blocks_wait"):
            rows.beside_state([(" | " if ascii else " · ", "dim")], [(reason, "")], width)
        blocker = watch_scene.row_of(snapshot.waiting_facts, number).get("blocker")
        if blocker:
            rows.add([(str(blocker), "")], "context")
        if run and run.get("detail"):
            rows.add([(run["detail"], "")], "detail")
        url = watch.issue_url(config, number)
        if url:
            # D112: the one GitHub link line; only this explicit span becomes an OSC 8 link.
            # Cut to the bubble, never wrapped: one link line, its label a prefix of the URL.
            rows.add(watch._fit([(url, "issue-link")], width), "link")
        return rows.keep_rows()

    if kind == "authored":
        return with_context(rows)
    if kind == "question":
        rows.add([(t("lane_bubble_question_click", who=t("question_person")), "question")], "state")
        return with_context(rows)
    if kind == "parked":
        rows.add([(t("lane_bubble_parked_click", lane=name), "")], "state")
        return with_context(rows)
    if kind == "next":
        rows.add([(t("lane_bubble_queued_click", lane=name), "")], "state")
        return with_context(rows)
    if kind == "wait" and item.get("asked") in {"orchestrator", "answered", "merge-owner"}:
        # A question's wait names whose turn it is, whatever phase (none, often) its run recorded.
        from foragent.watch_sections.lanes import row_words

        rows.add([(row_words(item["state"], phase, item["asked"], language)[1], "")], "state")
    elif phase == "merge-owner-wait":
        from foragent.messages.squirrel import phase_word

        rows.add([(phase_word(phase, language), "")], "state")
    elif kind == "wait":
        key = "blocked" if item["state"] == "blocked" and phase not in watch_scene.WAIT_PHASES else phase
        rows.add([(t(key), "")], "state")
    else:
        from foragent.messages.squirrel import phase_word

        rows.add([(phase_word(phase or "baseline", language), "")], "state")
    age = max(0.0, (now - snapshot.captured_at).total_seconds())
    # The step's own start; a run that recorded no step yet is in its first step since it began.
    started = (run.get("phase_started_at") or (run.get("started_at") if phase is None else None)) if run else None
    seconds = _age(started, snapshot.captured_at)
    if seconds is None and number == row.get("issue") and row.get("elapsed_seconds") is not None:
        seconds = row["elapsed_seconds"]
    if seconds is not None:
        rows.add([(t("lane_bubble_elapsed", elapsed=elapsed_text(seconds + age, language)), "")], "elapsed")
    if run:
        warning = watch_scene.activity_warning(snapshot, config, row, item, now)
        if warning:
            # The warning sits beside the step it is about, so a full bubble never drops it.
            rows.beside_state([(" | " if ascii else " · ", "dim")], [(warning, "yellow")], width)
    return with_context(rows)
