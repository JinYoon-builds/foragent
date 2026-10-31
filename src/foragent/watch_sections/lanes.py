"""The lane rows: one text row per running worker, else one waiting or idle row per lane (#167).

A row shows, in order: state glyph, lane, #issue and title, phase word, any
evidence warning, elapsed time and the five acorns. Columns align by terminal
cells; at 60–99 columns the title goes and below 60 the elapsed time too.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.status import _age
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot
from foragent.watch_layout import _clones
from foragent import watch_dream as dream

ORDER = 5
GAP = False  # The rows follow the one-line header directly.
KEEP = True  # Lane rows win when the screen is short.
TIGHT = True  # On a short plain frame, folded lanes first drop their extra rows.

GLYPHS = {
    "emoji": {"code": "🐿", "check": "🔍", "person": "✋", "wait": "⏳", "idle": "💤", "done": "🌰", "left": "○"},
    "ascii": {"code": "[*]", "check": "[?]", "person": "[!]", "wait": "[.]", "idle": "[z]", "done": "#", "left": "."},
}
CODE_PHASES = frozenset({"implementing", "resolving", "resolving-conflict"})
CHECK_WORDS = {"baseline": "squirrel_baseline", "checking": "squirrel_checking", "reviewing": "squirrel_reviewing",
               "ci": "squirrel_ci", "merging": "squirrel_merging"}
PERSON_PHASES = frozenset({"needs-human", "waiting-person"})
# The five acorns: code, local tests, review, CI and merge, each finished by its phase.
ACORNS = ("implementing", "checking", "reviewing", "ci", "merging")
# A baseline being measured (or awaited) is still unfinished.
BASELINE_PHASES = frozenset({"baseline", "baseline-wait"})
# The orchestrator's conflict fix comes after code, local tests and review.
RESOLVING_DONE = 3


def glyph_mode(config: Config) -> str:
    """emoji or ascii: an explicit choice wins; auto picks ascii for dumb/linux terminals or a non-UTF-8 locale."""
    from foragent import watch

    mode = getattr(config, "view_glyphs", "auto")
    if mode in GLYPHS:
        return mode
    environ = watch.os.environ
    if environ.get("TERM", "") in {"dumb", "linux"}:
        return "ascii"
    # The effective character set: LC_ALL, then LC_CTYPE, then LANG.
    locale = environ.get("LC_ALL") or environ.get("LC_CTYPE") or environ.get("LANG") or ""
    return "emoji" if "UTF8" in locale.upper().replace("-", "") else "ascii"


def _kind(state: str, phase: str | None) -> tuple[str, str]:
    """The glyph kind and phase-word key of one row's facts."""
    if state == "waiting-human" or phase in PERSON_PHASES:
        return "person", "squirrel_person"
    if state == "blocked":
        return "wait", "squirrel_orchestrator"
    if state == "queued":
        return "wait", "squirrel_turn"
    if phase in CHECK_WORDS:
        return "check", CHECK_WORDS[phase]
    if phase is None or phase in CODE_PHASES:
        return "code", "squirrel_code"
    return "wait", "squirrel_turn"  # Queue, slot, overlap and limit waits.


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _after(opened: datetime, started: datetime) -> bool:
    """Compare times that may mix naive and aware values (naive ones are UTC)."""
    aware = lambda moment: moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware(opened) >= aware(started)


def finished_acorns(phase: str | None, events: list[dict[str, Any]], started_at: Any = None,
                    waiting_from: str | None = None) -> set[int]:
    """The finished acorns of one run.

    With this run's closed phase events (those opened since the run started), a
    phase counts once it has one and stays earned. Local tests count once checking
    has closed and no baseline of the run is still unfinished: an open baseline
    (the current phase) holds the acorn back unless an earlier baseline already
    closed. Without any events the phases before the current one in the fixed order
    count; resolving counts code, local tests and review; a wait, a person or a
    blocked row counts those before ``waiting_from``, the phase its run was in.
    """
    # Only events that this run opened count: without its start time, none can be shown to be its own.
    started = _time(started_at)
    own = [event for event in events if started is not None
           and (opened := _time(event.get("phase_started_at"))) is not None and _after(opened, started)]
    if own:
        closed = {event["phase"] for event in own}
        done = {ACORNS.index(name) for name in closed if name in ACORNS and name != "checking"}
        if "checking" in closed and ("baseline" in closed or phase not in BASELINE_PHASES):
            done.add(ACORNS.index("checking"))
        return done
    current = phase
    if phase not in ACORNS and phase not in CODE_PHASES and phase not in CHECK_WORDS:
        current = waiting_from  # A wait keeps what its run had reached when it began waiting.
    if current in ACORNS:
        return set(range(ACORNS.index(current)))
    if current in CODE_PHASES - {"implementing"}:
        return set(range(RESOLVING_DONE))
    return set()


def elapsed_text(seconds: float | None, language: str) -> str:
    from foragent import watch

    if seconds is None:
        return ""
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return watch.text("squirrel_minutes", language, minutes=minutes)
    return watch.text("squirrel_hours", language, hours=minutes // 60, minutes=minutes % 60)


def _runner_progress(run: dict[str, Any] | None, captured_at: Any) -> float | None:
    """Seconds since the runner's own last progress, or None before it made any.

    start_run sets last_progress_at to the start time; that value is the default,
    not evidence that the runner moved.
    """
    if not run or run.get("last_progress_at") in (None, run.get("started_at")):
        return None
    return _age(run["last_progress_at"], captured_at)


def _issue_row(snapshot: Snapshot, config: Config, row: dict[str, Any], name: str, number: int,
               run: dict[str, Any] | None, state: str, phase: str | None, title: str,
               glyphs: dict[str, str], now: datetime) -> dict[str, Any]:
    """One worker, waiting or queued issue as column values."""
    from foragent import watch

    language = config.language
    kind, words = _kind(state, phase)
    age = max(0.0, (now - snapshot.captured_at).total_seconds())
    # The run behind the row: the active one, else (except for a queued issue, which has no
    # run yet) the issue's latest recorded run from the full history, then status.history.
    past = None if run or state == "queued" else next(
        (item for item in reversed(snapshot.status.get("history") or []) if item.get("number") == number), {})
    started = (run or {}).get("started_at") if run else None if past is None else (
        snapshot.run_starts.get(number) or past.get("started_at"))
    waiting_from = None if past is None else (
        snapshot.run_phases[number] if number in snapshot.run_phases else past.get("phase"))
    elapsed = None
    if _time(started) is not None:
        elapsed = (now - _time(started)).total_seconds()
    elif state != "queued" and number == row.get("issue") and row.get("elapsed_seconds") is not None:
        elapsed = row["elapsed_seconds"] + age
    activity = ""
    if run:
        # The lane's own issue keeps the status figure; another worker reads its run.
        quiet = (row.get("last_activity_seconds") if number == row.get("issue")
                 else _age(run.get("last_activity_at") or run.get("started_at"), snapshot.captured_at))
        shown = {**row, "state": state, "phase": phase, "last_activity_seconds": quiet}
        found = watch._activity(
            shown, language, age, config.stall_minutes, run.get("pid"), snapshot.log_ages.get(number),
            _runner_progress(run, snapshot.captured_at), _age(run.get("phase_started_at"), snapshot.captured_at))
        # The row's phase word and time already say what is normal; only warnings add a cell.
        activity = found if found.startswith(("○", "?")) else ""
    # Acorns belong to that one run; without it nothing was earned, so older events never count.
    events = snapshot.phases.get(number, []) if _time(started) is not None else []
    done = finished_acorns(phase, events, started, waiting_from)
    acorns = "".join(glyphs["done" if index in done else "left"] for index in range(len(ACORNS)))
    return {"glyph": glyphs[kind], "lane": name, "number": f"#{number}", "issue": number, "title": watch._clean(title or ""),
            "words": watch.text(words, language), "activity": activity,
            # Without a recorded time (a queued issue, a row with no run) the cell says so.
            "elapsed": elapsed_text(elapsed, language) or "—", "acorns": acorns,
            "style": "yellow" if activity or kind == "person" else ""}


def _finished_row(snapshot: Snapshot, config: Config, issue: Any, name: str,
                  glyphs: dict[str, str]) -> dict[str, Any]:
    """A merged issue: its latest run's start to its merge, every acorn earned."""
    from foragent import watch

    started, merged = _time(snapshot.run_starts.get(issue.number)), snapshot.merged_at.get(issue.number)
    elapsed = "—"
    if started is not None and merged is not None:
        aware = lambda moment: moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        elapsed = elapsed_text(max(0.0, (aware(merged) - aware(started)).total_seconds()), config.language) or "—"
    return {"glyph": glyphs["idle"], "lane": name, "number": f"#{issue.number}", "issue": issue.number,
            "title": watch._clean(issue.title or ""), "words": watch.text("acorns_done", config.language),
            "activity": "", "elapsed": elapsed, "acorns": glyphs["done"] * len(ACORNS), "style": "dim"}


def last_finished(snapshot: Snapshot, issues: list[Any]) -> Any:
    """The latest merge by merged_at; without any merge time, the done list's last entry."""
    timed = [issue for issue in issues if snapshot.merged_at.get(issue.number) is not None]
    if not timed:
        return issues[-1] if issues else None
    aware = lambda moment: moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return max(timed, key=lambda issue: aware(snapshot.merged_at[issue.number]))


def _rows(snapshot: Snapshot, config: Config, glyphs: dict[str, str], now: datetime,
          unfolded: frozenset[str] | set[str] = frozenset(), tight: bool = False) -> list[dict[str, Any]]:
    """Every lane's rows in lane order, then its fold toggle when rows are hidden.

    Unfolded, a lane lists its finished issues, its workers (blocked and waiting
    for a person count as running) and every waiting issue. Folded, it keeps the
    last finished, the running rows and the next waiting issue; an idle lane folds
    to its one summary row. ``tight`` (a short plain frame) leaves out a folded
    lane's last finished row and toggle, so the frame keeps today's rows; a frame with
    or without view state draws the same rows. The toggle shows whenever a lane has
    hidden rows, whether or not the order is finished.
    """
    from foragent import watch

    status, language = snapshot.status, config.language
    clones = _clones(status)
    found: list[dict[str, Any]] = []
    for row in status["lanes"].values():
        slug, name = row["slug"], row["lane_display"]
        is_open = slug in unfolded
        workers = clones.get(slug, [])
        titles = {item["number"]: item.get("title") for item in row.get("running", []) + row.get("queued", [])}
        active: list[dict[str, Any]] = []
        if workers:
            for position, run in enumerate(workers, 1):
                number = run["number"]
                own = number == row.get("issue")
                state = snapshot.worker_states.get(number) or (row["state"] if own else "working")
                phase = run.get("phase") or (row.get("phase") if own else None)
                # Status lists only some workers; the local issue records name every one.
                title = titles.get(number) or snapshot.titles.get(number) or (row.get("title") if own else "") or ""
                label = f"{name} {position}" if len(workers) > 1 else name
                active.append(_issue_row(snapshot, config, row, label, number, run, state, phase, title, glyphs, now))
        # Blocked and waiting-for-a-person issues without a run count as running too:
        # every one the status lists ("held"), then the lane's own issue for older snapshots.
        held = [dict(item) for item in row.get("held", [])]
        if row.get("issue") is not None and row["state"] in {"working", "waiting-human", "blocked"} and not workers:
            held.append({"number": row["issue"], "title": row.get("title"), "state": row["state"],
                         "phase": row.get("phase")})
        shown = {run["number"] for run in workers}
        for item in held:
            number = item["number"]
            if number in shown:
                continue
            shown.add(number)
            # A working issue whose run has stopped keeps the phase that run recorded.
            phase = item.get("phase") or (snapshot.run_phases.get(number) if item["state"] == "working" else None)
            active.append(_issue_row(snapshot, config, row, name, number, None, item["state"], phase,
                                     item.get("title") or snapshot.titles.get(number) or "", glyphs, now))
        shown = {item["issue"] for item in active}
        queued = [item for item in row.get("queued", []) if item["number"] not in shown]
        authored = {number: fact for number, fact in snapshot.waiting_facts.items()
                    if fact.get("authored") and fact.get("lane") == slug}
        listed = shown | {item["number"] for item in queued}
        queued += [{"number": number, "title": fact.get("title", "")}
                   for number, fact in authored.items() if number not in listed]
        current = shown | {item["number"] for item in queued}
        # A reopened issue can have a past merge and a current wait. Its current row
        # carries the explanation; the historical row must not repeat it as done.
        finished = [issue for issue in snapshot.done.get(slug, []) if issue.number not in current]
        rows: list[dict[str, Any]] = []
        if not active and not queued:
            # Today's merges in this lane; a snapshot built without them keeps the order's done list.
            count = (snapshot.today_done.get(slug, 0) if snapshot.today_done is not None
                     else len(snapshot.done.get(slug, [])))
            rest = "✓ " + watch.text("done_state_lane_done", language, count=count) if count else ""
            rows.append(dream.idle_row(row, glyphs["idle"], glyph_mode(config), rest, language))
            hideable = len(finished)
            if is_open:
                rows += [_finished_row(snapshot, config, issue, name, glyphs) for issue in finished]
        else:
            folded_queue = [item for index, item in enumerate(queued) if index == 0 or item["number"] in authored]
            visible_queue = queued if is_open else folded_queue
            hideable = max(0, len(finished) - 1) + len(queued) - len(folded_queue)
            last = last_finished(snapshot, finished)
            for issue in finished if is_open else [last] if last is not None and not tight else []:
                rows.append(_finished_row(snapshot, config, issue, name, glyphs))
            rows += active
            for item in visible_queue:
                # A queued issue has no run: neither the lane's phase nor its acorns are its own.
                waiting = _issue_row(snapshot, config, row, name, item["number"], None, "queued", None,
                                     item.get("title") or snapshot.titles.get(item["number"]) or "", glyphs, now)
                if item["number"] in authored:
                    waiting["words"] = watch.text("waiting_view_state", language)
                rows.append({**waiting, "style": "dim"})
        found += [{**item, "slug": slug} for item in rows]
        if hideable and (is_open or not tight):
            words = (watch.text("acorns_fold", language) if is_open
                     else watch.text("acorns_more", language, count=hideable))
            found.append({"toggle": slug, "text": words})
    return found


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    state = context.get("state")
    if state is not None and state.get("issue") is not None:
        return []  # The open issue box (watch_sections/acorns) stands in for the rows.
    width = context["width"]
    glyphs = GLYPHS[glyph_mode(config)]
    unfolded = set(state.get("unfolded", ())) if state is not None else set()
    rows = _rows(snapshot, config, glyphs, context["now"], unfolded, bool(context.get("tight")))
    if not rows:
        return []
    cells = [row for row in rows if "toggle" not in row]
    size = lambda key: max((watch._cells(row.get(key, "")) for row in cells if "number" in row), default=0)
    glyph_width = max(watch._cells(value) for value in glyphs.values())
    # Worker lanes share one column; an idle row keeps its whole name.
    lane_width = min(max(10, width // 4), max(watch._cells(row["lane"]) for row in cells))
    columns = ["glyph", "lane", "number", "title", "words", "activity", "elapsed", "acorns"]
    widths = {"glyph": glyph_width, "lane": lane_width, **{key: size(key) for key in columns[2:]}}
    if width < 60:
        widths["elapsed"] = 0
    if width < 100:
        widths["title"] = 0
    else:
        others = sum(value + 1 for key, value in widths.items() if key != "title" and value)
        widths["title"] = min(widths["title"], max(0, width - others - 1))
    # The row must fit: warnings, then the lane name, then the phase word give way, so the
    # glyph, number, elapsed time and acorns at the end are never pushed off the line.
    for key, least in (("activity", 0), ("lane", 4), ("words", 4), ("lane", 1), ("words", 1)):
        over = sum(value + 1 for value in widths.values() if value) - 1 - width
        if over > 0:
            widths[key] = max(min(widths[key], least), widths[key] - over)
    shown = [key for key in columns if widths[key]]
    hits, marks = context.get("hits"), context.get("marks")
    lines: list[Line] = []
    for row in rows:
        if "toggle" in row:
            # The toggle sits under the lane's rows, in the lane column.
            text = watch._clean(row["text"])
            lines.append(watch._fit(watch._line(" " * (glyph_width + 1) + text, "dim"), width))
            target = (glyph_width + 1, len(lines) - 1, min(width, glyph_width + 1 + watch._cells(text)),
                      len(lines), ("more", row["toggle"]))
        elif "number" in row:
            lines += watch._columns([row[key] for key in shown], [widths[key] for key in shown], row["style"])
            # The whole row, from its #issue to its acorns, opens the issue's box.
            target = (0, len(lines) - 1, min(width, watch._cells("".join(v for v, _ in lines[-1]).rstrip())),
                      len(lines), ("acorn", row["issue"]))
            if marks is not None:
                marks.append((len(lines) - 1, row["issue"], row["slug"]))  # A lane row folds its lane.
        else:
            values = [row["glyph"], row["lane"]] + ([row["rest"]] if row["rest"] else [])
            sizes = [glyph_width, max(lane_width, watch._cells(row["lane"]))] + (
                [watch._cells(row["rest"])] if row["rest"] else [])
            lines += watch._columns(values, sizes)
            continue
        if hits is not None:
            hits.append(target)
        if "issue" in row:
            authored = snapshot.waiting_facts.get(row["issue"], {}).get("authored")
            if authored:
                from foragent.watch_waiting import explanation

                lines += explanation(authored, config.language, width)
    return lines
