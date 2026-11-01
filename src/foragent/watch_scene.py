"""Facts the lane scene (lane_bubble and lane_blocks) shares: each lane's acorns and their class.

The lane blocks draw these acorns and the bubble explains them, so both read the same
items and the same class from one helper. Everything comes from the snapshot: status
lane rows, active runs, the needs-human group, park facts and waiting facts. This
module lives outside watch_sections, so discovery never treats it as a section.
"""

from datetime import datetime
from typing import Any

from foragent.config import Config
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot
from foragent.watch_layout import _clones

# Recorded wait phases: the run waits for a turn, a slot, a baseline, a limit or the model.
WAIT_PHASES = frozenset({"overlap-wait", "test-slot-wait", "baseline-wait", "limit-wait", "executor-wait"})
# The step word (and click phrase) of each working phase.
STEPS = {"baseline": "baseline", "implementing": "implementing", "resolving": "implementing",
         "checking": "checking", "reviewing": "reviewing", "ci": "ci", "merging": "merging",
         "merge-lock-wait": "merging", "resolving-conflict": "merging"}
# Acorn classes in the order a lane block is placed by.
CATEGORIES = ("working", "question", "waiting", "idle")


def fit_marked(line: Line, width: int, mark: str, *, omitted: bool = False) -> Line:
    """``line`` within ``width`` cells; when anything is cut away it ends in ``mark``.

    watch._fit clips silently, which hides that a name, path or count was shortened.
    ``omitted`` also marks a fitting row when later rows were left out.
    """
    from foragent import watch

    if not omitted and sum(watch._cells(value) for value, _ in line) <= width:
        return list(line)
    room = width - watch._cells(mark)
    if room < 0:
        return watch._fit([(mark, "")], width)
    kept = watch._fit(line, room)
    while kept:
        value, style = kept[-1]
        if value.rstrip():
            kept[-1] = (value.rstrip(), style)
            break
        kept.pop()
    style = next((style for value, style in reversed(kept) if value.strip()), "")
    return [*kept, (mark, style)]


def text(key: str, language: str, *, ascii: bool = False, **values) -> str:
    """Choose a template before formatting, preserving literal names and paths."""
    from foragent import i18n, watch

    catalog = i18n.MESSAGES.get(language.lower().split("-")[0], i18n.MESSAGES["en"])
    if ascii and key + "_ascii" in catalog:
        key += "_ascii"
    return watch.text(key, language, **values)


def generated_ascii(value: str) -> str:
    """ASCII decorations for generated status copy; never apply to authored data."""
    return value.translate(str.maketrans({"·": "|", "⚠": "!", "○": "o", "—": "-", "…": "..."}))


def questions(status: dict[str, Any]) -> set[int]:
    """Distinct issue numbers in the needs-human group (the header counts the same)."""
    issues = status.get("issues")
    group = issues.get("needs-human") if isinstance(issues, dict) else None
    return {item["number"] for item in group or () if isinstance(item, dict) and type(item.get("number")) is int}


def acorn_class(snapshot: Snapshot, row: dict[str, Any], number: int, state: str, phase: str | None,
                needs: set[int]) -> str:
    """question, parked, wait, working, authored or next: the one rule for an acorn.

    Recorded question evidence wins (the item's own state, the needs-human group, or the
    lane's representative row in waiting-human, for that issue only), then the latest park
    fact, then a recorded wait phase or an orchestrator block, then work. Queued
    authored waits keep their own context; ordinary queued work is next.
    """
    if (state == "waiting-human" or number in needs
            or (number == row.get("issue") and row.get("state") == "waiting-human")):
        return "question"
    if (snapshot.park_facts.get(number) or {}).get("parked"):
        return "parked"
    if state == "queued":
        return "authored" if (snapshot.waiting_facts.get(number) or {}).get("authored") else "next"
    if phase in WAIT_PHASES or state == "blocked":
        return "wait"
    return "working"


def lane_items(snapshot: Snapshot, row: dict[str, Any], needs: set[int] | None = None,
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The lane's current acorns and its next ones, each with number, title, state, phase,
    run (the active run or None) and class.

    Current work is every issue the row lists as running, every running run of the lane,
    the held issues (blocked or waiting for a person) and the row's own issue when it is
    working, held or blocked. Next is the queued list without those.
    """
    status = snapshot.status
    needs = questions(status) if needs is None else needs
    runs = {run["number"]: run for run in _clones(status).get(row.get("slug"), [])}
    # Only a lane in this snapshot can redirect a status entry. Unresolved optional
    # references leave the status's own placement intact.
    slugs = {lane["slug"] for lane in status["lanes"].values()}
    authored = {number: fact for number, fact in snapshot.waiting_facts.items()
                if fact.get("authored") and fact.get("lane") in slugs}
    titles = {item["number"]: item.get("title") for key in ("running", "queued", "held")
              for item in row.get(key) or () if isinstance(item, dict) and "number" in item}
    current: list[dict[str, Any]] = []
    seen: dict[int, dict[str, Any]] = {}

    def add(target: list[dict[str, Any]], number: int, state: str, phase: str | None,
            run: dict[str, Any] | None) -> None:
        fact = snapshot.waiting_facts.get(number) or {}
        if number in authored and authored[number]["lane"] != row.get("slug"):
            return  # The recorded active lane wins over a subsequently edited label.
        if number in seen:
            # One acorn per issue, but question evidence from any listing still counts.
            entry = seen[number]
            if state == "waiting-human" and entry["state"] != "waiting-human":
                entry["state"] = state
                entry["class"] = acorn_class(snapshot, row, number, state, entry["phase"], needs)
            return
        title = titles.get(number) or snapshot.titles.get(number) or fact.get("title") or (
            row.get("title") if number == row.get("issue") else "") or ""
        entry = {"number": number, "title": title, "state": state, "phase": phase, "run": run,
                 "class": acorn_class(snapshot, row, number, state, phase, needs), "authored": fact.get("authored")}
        seen[number] = entry
        target.append(entry)

    def worker(number: int, run: dict[str, Any] | None, listed: str | None = None) -> None:
        own = number == row.get("issue")
        state = snapshot.worker_states.get(number) or (row.get("state") if own else "working") or "working"
        if listed == "waiting-human":
            state = listed  # The listing's own question wins over any other state.
        phase = (run or {}).get("phase") or (row.get("phase") if own else None)
        add(current, number, state, phase, run)

    # Running runs first, in start order (ties by issue number), then any other running issue.
    listed = {item["number"]: item.get("state") for item in row.get("running") or ()
              if isinstance(item, dict) and isinstance(item.get("state"), str)}
    for number, run in runs.items():
        worker(number, run, listed.get(number))
    for item in row.get("running") or ():
        worker(item["number"], runs.get(item["number"]), listed.get(item["number"]))
    for item in row.get("held") or ():
        number, state = item["number"], item.get("state") or "blocked"
        phase = item.get("phase") or (snapshot.run_phases.get(number) if state == "working" else None)
        add(current, number, state, phase, None)
    number = row.get("issue")
    if number is not None and row.get("state") in {"working", "waiting-human", "blocked"}:
        # A row whose run has stopped keeps the phase that run recorded.
        state = row["state"]
        phase = row.get("phase") or (snapshot.run_phases.get(number) if state == "working" else None)
        add(current, number, state, phase, None)
    upcoming: list[dict[str, Any]] = []
    for item in row.get("queued") or ():
        # A queued item keeps a recorded question; any other state of the queue is next.
        add(upcoming, item["number"], "waiting-human" if item.get("state") == "waiting-human" else "queued",
            None, None)
    # An authored wait is visible even without approval or a scheduler queue entry.
    # add() keeps a listed worker/held/queued acorn and its class instead of duplicating it.
    for number, fact in authored.items():
        if fact["lane"] == row.get("slug"):
            add(upcoming, number, "queued", None, None)
    return current, upcoming


def category(current: list[dict[str, Any]], upcoming: list[dict[str, Any]]) -> str:
    """working, question, waiting or idle: where the lane's block goes."""
    classes = {item["class"] for item in current}
    if "working" in classes:
        return "working"
    # A queued issue with a recorded question asks too: its class, not its listing, decides.
    if "question" in classes or any(item["class"] == "question" for item in upcoming):
        return "question"
    return "waiting" if current or upcoming else "idle"


def representative_wait(row: dict[str, Any], current: list[dict[str, Any]],
                        upcoming: list[dict[str, Any]]) -> int | None:
    """The issue whose waiting reason the lane shows, or None.

    The lane's representative (the row's own issue) in a recorded wait phase or an
    orchestrator block always has one. A lane with nothing working or asked (in its current
    or queued acorns) shows the reason of its first waiting acorn, else of its first queued
    acorn classed next (its own issue first); parked and question acorns wait for no turn.
    """
    # Current wait states keep their sentence even when they also have authored context.
    waiting = [item["number"] for item in current if item["class"] == "wait"]
    if row.get("issue") in waiting:
        return row["issue"]
    if any(item["class"] in {"working", "question"} for item in current + upcoming):
        return None
    if waiting:
        return waiting[0]
    # Only acorns classed as next wait their turn; parked and question ones never do.
    turn = [item for item in upcoming if item["class"] == "next"]
    if not turn:
        return None
    chosen = next((item for item in turn if item["number"] == row.get("issue")), turn[0])
    return chosen["number"]


def waiting_reason(snapshot: Snapshot, row: dict[str, Any], number: int, language: str, *, ascii: bool = False) -> str:
    """The waiting sentence, with a cause only when facts record one.

    Recorded waiting facts come first; a snapshot without them still names the status's
    recorded blockers of the row's own issue.
    """
    facts = snapshot.waiting_facts.get(number)
    if facts is None:
        facts = {"kind": None, "blocking_issues": list(row.get("waiting_for") or [])
                 if number == row.get("issue") else [], "overlapping_files": []}
    kind, blockers = facts.get("kind"), [n for n in facts.get("blocking_issues") or () if type(n) is int]
    t = lambda key, **values: text(key, language, ascii=ascii, **values)
    if blockers:
        return t("lane_blocks_wait_after", issues=", ".join(f"#{n}" for n in blockers))
    if kind == "capacity":
        return t("lane_blocks_wait_slots")
    if kind == "machine":
        return t("lane_blocks_wait_machine")
    if kind == "file-overlap":
        paths = [str(path) for path in facts.get("overlapping_files") or ()]
        reason = (t("lane_blocks_wait_overlap", paths=", ".join(paths)) if paths
                  else t("lane_blocks_wait_overlap_unknown"))
        # Facts name the overlap first (its kind order); a queued issue whose slots the
        # collected meter also records as full waits for both, so both are shown.
        queued = any(isinstance(item, dict) and item.get("number") == number for item in row.get("queued") or ())
        return t("lane_blocks_wait_and_slots", reason=reason) if queued and slots_full(snapshot.meter) else reason
    return t("lane_blocks_wait")


def slots_full(meter: Any) -> bool:
    """The collected meter records every slot in use: numeric running >= a positive budget."""
    if not isinstance(meter, dict):
        return False
    running, budget = meter.get("running"), meter.get("budget")
    numeric = all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (running, budget))
    return numeric and budget > 0 and running >= budget


def activity_warning(snapshot: Snapshot, config: Config, row: dict[str, Any], item: dict[str, Any],
                     now: datetime) -> str:
    """A running acorn's quiet, stalled or missing-worker warning from watch._activity, else ''."""
    from foragent import watch
    from foragent.status import _age
    from foragent.watch_sections.lanes import _runner_progress

    run, number = item["run"], item["number"]
    if not run:
        return ""
    age = max(0.0, (now - snapshot.captured_at).total_seconds())
    # The lane's own issue keeps the status figure when the status has one (a queued or held
    # representative has none); otherwise the worker's own run says when it last moved.
    quiet = row.get("last_activity_seconds") if number == row.get("issue") else None
    if quiet is None:
        quiet = _age(run.get("last_activity_at") or run.get("started_at"), snapshot.captured_at)
    shown = {**row, "state": item["state"], "phase": item["phase"], "last_activity_seconds": quiet}
    found = watch._activity(
        shown, config.language, age, config.stall_minutes, run.get("pid"), snapshot.log_ages.get(number),
        _runner_progress(run, snapshot.captured_at), _age(run.get("phase_started_at"), snapshot.captured_at))
    # The acorn already says what is normal; only warnings add words.
    if not found.startswith(("○", "?")):
        return ""
    from foragent.watch_sections.lanes import glyph_mode

    return generated_ascii(found) if glyph_mode(config) == "ascii" else found
