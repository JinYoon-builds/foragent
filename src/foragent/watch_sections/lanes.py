"""The lane rows: one text row per running worker, else one waiting or idle row per lane (#167).

A row shows, in order: state glyph, lane, #issue and title, phase word, any
evidence warning, elapsed time and the five acorns. Columns align by terminal
cells; at 60–99 columns the title goes and below 60 the elapsed time too.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.status import _age
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot
from foragent.watch_layout import _clones

ORDER = 5
GAP = False  # The rows follow the one-line header directly.
KEEP = True  # Lane rows win when the screen is short.

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


def finished_acorns(phase: str | None, events: list[dict[str, Any]], started_at: Any = None) -> set[int]:
    """The finished acorns of one run.

    With this run's closed phase events (those opened since the run started), a
    phase counts once it has one; local tests also need any baseline to be over.
    Without any, the phases before the current one in the fixed order count;
    resolving counts code, local tests and review, and waits count none.
    """
    started = _time(started_at)
    own = [event for event in events
           if started is None or (opened := _time(event.get("phase_started_at"))) is None or opened >= started]
    if own:
        done = {ACORNS.index(event["phase"]) for event in own if event["phase"] in ACORNS}
        if phase == "baseline":
            done.discard(1)
        return done
    if phase in ACORNS:
        return set(range(ACORNS.index(phase)))
    if phase in CODE_PHASES - {"implementing"}:
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
    elapsed = None
    if run and _time(run.get("started_at")) is not None:
        elapsed = (now - _time(run["started_at"])).total_seconds()
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
    done = finished_acorns(phase, snapshot.phases.get(number, []), run.get("started_at") if run else None)
    acorns = "".join(glyphs["done" if index in done else "left"] for index in range(len(ACORNS)))
    return {"glyph": glyphs[kind], "lane": name, "number": f"#{number}", "title": watch._clean(title or ""),
            "words": watch.text(words, language), "activity": activity,
            "elapsed": elapsed_text(elapsed, language), "acorns": acorns,
            "style": "yellow" if activity or kind == "person" else ""}


def _rows(snapshot: Snapshot, config: Config, glyphs: dict[str, str], now: datetime) -> list[dict[str, Any]]:
    """Every lane's rows in lane order: its workers, else its waiting or next queued issue, else idle."""
    from foragent import watch

    status, language = snapshot.status, config.language
    clones = _clones(status)
    found: list[dict[str, Any]] = []
    for row in status["lanes"].values():
        slug, name = row["slug"], row["lane_display"]
        workers = clones.get(slug, [])
        titles = {item["number"]: item.get("title") for item in row.get("running", []) + row.get("queued", [])}
        if workers:
            for position, run in enumerate(workers, 1):
                number = run["number"]
                own = number == row.get("issue")
                state = snapshot.worker_states.get(number) or (row["state"] if own else "working")
                phase = run.get("phase") or (row.get("phase") if own else None)
                title = titles.get(number) or (row.get("title") if own else "") or ""
                label = f"{name} {position}" if len(workers) > 1 else name
                found.append(_issue_row(snapshot, config, row, label, number, run, state, phase, title, glyphs, now))
        elif row.get("issue") is not None and row["state"] in {"working", "waiting-human", "blocked"}:
            found.append(_issue_row(snapshot, config, row, name, row["issue"], None, row["state"],
                                    row.get("phase"), row.get("title") or "", glyphs, now))
        elif row.get("queued"):
            item = row["queued"][0]
            found.append(_issue_row(snapshot, config, row, name, item["number"], None, "queued",
                                    row.get("phase"), item.get("title") or "", glyphs, now))
        else:
            count = len(snapshot.done.get(slug, []))
            rest = "✓ " + watch.text("done_state_lane_done", language, count=count) if count else ""
            found.append({"glyph": glyphs["idle"], "lane": name, "rest": rest})
    return found


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    width = context["width"]
    glyphs = GLYPHS[glyph_mode(config)]
    rows = _rows(snapshot, config, glyphs, context["now"])
    if not rows:
        return []
    size = lambda key: max((watch._cells(row.get(key, "")) for row in rows if "number" in row), default=0)
    glyph_width = max(watch._cells(value) for value in glyphs.values())
    # Worker lanes share one column; an idle row keeps its whole name.
    lane_width = min(max(10, width // 4), max(watch._cells(row["lane"]) for row in rows))
    columns = ["glyph", "lane", "number", "title", "words", "activity", "elapsed", "acorns"]
    widths = {"glyph": glyph_width, "lane": lane_width, **{key: size(key) for key in columns[2:]}}
    if width < 60:
        widths["elapsed"] = 0
    if width < 100:
        widths["title"] = 0
    else:
        others = sum(value + 1 for key, value in widths.items() if key != "title" and value)
        widths["title"] = min(widths["title"], max(0, width - others - 1))
    shown = [key for key in columns if widths[key]]
    lines: list[Line] = []
    for row in rows:
        if "number" in row:
            lines += watch._columns([row[key] for key in shown], [widths[key] for key in shown], row["style"])
        else:
            values = [row["glyph"], row["lane"]] + ([row["rest"]] if row["rest"] else [])
            sizes = [glyph_width, max(lane_width, watch._cells(row["lane"]))] + (
                [watch._cells(row["rest"])] if row["rest"] else [])
            lines += watch._columns(values, sizes)
    return lines
