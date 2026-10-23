"""Lane conveyor layout and the plain-text and detail views.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.status import _age, summary_values, unknown as status_unknown
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot


# Only the pickaxe moves. One hit every four 0.3-second frames.
PICK = ("  ⛏ ", " ⛏  ", "⛏✦  ", " ⛏  ")


def render_detail(config: Config, slug: str, repo_root: Path, done_count: int = 0) -> str:
    """Show lane configuration and exactly the first three memory lines."""
    from foragent import watch

    language = config.language
    lane = config.lanes.get(slug, {})
    executor = lane.get("executor") or config.executor
    model = lane.get("model") or config.executor_model or watch.text("watch_default_model", language)
    none = watch.text("watch_none", language)
    lines = [
        f"{watch.text('watch_lane', language)}: {watch.lane_display(config, slug, language)} ({slug})",
        f"{watch.text('watch_paths', language)}: {', '.join(lane.get('paths', [])) or none}",
        f"{watch.text('watch_executor', language)}: {executor}",
        f"{watch.text('watch_model', language)}: {model}",
        f"{watch.text('watch_skills', language)}: {', '.join(lane.get('skills', [])) or none}",
        watch.text("watch_done_count", language, count=done_count),
        watch.text("watch_memory", language) + ":",
    ]
    agent = lane.get("agent") or f"fa-{slug}"
    memory = watch.Path(repo_root) / ".claude" / "agent-memory" / agent / "MEMORY.md"
    try:
        with memory.open(encoding="utf-8", errors="replace") as stream:
            lessons = [line.rstrip("\n") for line in watch.islice(stream, 3)]
    except FileNotFoundError:
        lessons = []
    lines.extend(lessons or [watch.text("watch_no_memory", language)])
    return "\n".join(watch._clean(line) for line in lines)


def _has_work(row: dict[str, Any]) -> bool:
    return (row["issue"] is not None or bool(row["queued"])
            or row["state"] in {"working", "blocked", "waiting-human"})


def _activity(
    row: dict[str, Any], language: str, age: float, stall_minutes: float, pid: int | None,
) -> str:
    from foragent import watch

    if row["state"] == "blocked":
        return "● " + watch.text("blocked", language)
    if row["state"] != "working":
        return ""
    if pid is not None:
        try:
            watch.os.kill(pid, 0)
        except ProcessLookupError:
            return watch.text("watch_runner_gone", language)
        except PermissionError:
            pass  # A process owned by another user still exists.
    seconds = row["last_activity_seconds"]
    if seconds is not None and seconds + age > min(10, stall_minutes) * 60:
        return watch.text("watch_stalled", language, minutes=int((seconds + age) // 60))
    return "●"


def _frame(
    snapshot: Snapshot, config: Config, repo_root: Path, *, tick: int, width: int,
    selected: str | None, now: datetime,
) -> list[Line]:
    from foragent import watch

    language = config.language
    status = snapshot.status
    rows = list(status["lanes"].values())
    busy = [(index, row) for index, row in enumerate(rows, 1) if watch._has_work(row)]
    idle = [(index, row) for index, row in enumerate(rows, 1) if not watch._has_work(row)]
    t = lambda key, **values: watch.text(key, language, **values)
    side_by_side = width >= 100
    orchestrator_width = 28 if side_by_side else width
    road_width = width - orchestrator_width - 2 if side_by_side else width
    state = "watch_handling" if status["summary"]["blocked"] else "watch_monitoring"
    overview = [watch._line(t(state), "yellow" if status["summary"]["blocked"] else "green")]
    if snapshot.order:
        filled = round(10 * snapshot.completed / snapshot.total) if snapshot.total else 0
        overview += [watch._line(t("watch_order") + f" #{snapshot.order.number}"),
                     watch._line(snapshot.order.title),
                     watch._line("▓" * filled + "░" * (10 - filled) + f" {snapshot.completed}/{snapshot.total}", "green")]
    else:
        overview.append(watch._line(t("watch_no_order")))
    overview.append(watch._line(t("watch_waiting", **summary_values({"count": status["summary"]["waiting_human"]})), "bold"))
    eta = (t("watch_complete") if snapshot.order and snapshot.completed == snapshot.total
           else t("watch_eta", time="~" + snapshot.eta.astimezone().strftime("%H:%M") if snapshot.eta
                  else t("watch_estimating") if snapshot.order else "—"))
    overview.append(watch._line(eta))
    left = watch._panel(overview, orchestrator_width, "🧭 " + t("watch_orchestrator"))
    roads: list[Line] = [[("━" * road_width, "edge")]]
    # Several runs in one lane are clones of its agent: name the lane once with ×N.
    clones: dict[str, list[dict[str, Any]]] = {}
    for run in sorted(status["active_runs"], key=lambda run: run["number"]):
        clones.setdefault(run["lane"], []).append(run)

    def lane_name(row: dict[str, Any]) -> str:
        count = len(clones.get(row["slug"], []))
        return row["lane_display"] + (f" ×{count}" if count > 1 else "")

    def lane_issues(row: dict[str, Any]) -> list[int]:
        current = row["issue"] if row["state"] in {"working", "blocked", "waiting-human"} else None
        numbers = [current] if current is not None else []
        return numbers + [run["number"] for run in clones.get(row["slug"], []) if run["number"] not in numbers]

    for index, row in busy:
        slug, state = row["slug"], row["state"]
        lane = config.lanes.get(slug, {})
        executor = lane.get("executor") or config.executor
        color, badge = ("claude", "✳") if executor == "claude" else ("codex", "✺")
        display = lane_name(row)
        # The lane title (without its decorative emoji) names its responsible agent.
        title = lane.get("title") or watch.lane_display(config, slug, language).removeprefix("🧰 ")
        prefix = watch._fit(watch._line(f"{index} {display} ▸ ", "bold"), min(19, road_width // 3))
        queue = row["queued"]
        label_width = min(25, max(8, road_width // 3))
        agent_label = "".join(value for value, style in watch._fit(watch._line(t('watch_agent', lane=title)), label_width)).rstrip()
        label = f"▐ {badge} {agent_label} ▌"
        tool = watch.PICK[tick % 4] if state == "working" else " ✋ " if state == "waiting-human" else " ●  " if state == "blocked" else " zZ "
        hit = "hit" if state == "working" and tick % 4 == 2 else color
        tail: Line = [(f" #{number} ", hit) for number in lane_issues(row)]
        tail += [(tool, "yellow"), (label, color)]
        space = max(0, road_width - sum(watch._cells(value) for value, style in prefix + tail))
        shown = len(queue)
        while True:
            road: Line = []
            if shown < len(queue):
                road.append((f"+{len(queue) - shown} ", ""))
            for item in reversed(queue[:shown]):
                road += [(f" #{item['number']} ", "queue"), (" ", "road")]
            road_cells = sum(watch._cells(value) for value, style in road)
            if road_cells <= space or shown == 0:
                break
            shown -= 1
        roads.append(watch._fit([*prefix, (" " * max(0, space - road_cells), "road"), *road, *tail], road_width))
        done = snapshot.done.get(slug, [])
        roads.append(watch._fit(watch._line("  " + t("watch_done", count=len(done), title=done[-1].title if done else "—"), "green"), road_width))
        last = index == busy[-1][0] and not idle
        edge = "━" * road_width if last else ("▬▬▬   " * (road_width // 6 + 1))[:road_width]
        roads.append([(edge, "edge" if last else "divider")])
    if idle:
        items = []
        for index, row in idle:
            count = len(snapshot.done.get(row["slug"], []))
            items.append(f"{index} {row['lane_display']}" + (f" ✓{count}" if count else ""))
        roads.append(watch._fit(watch._line(t("watch_idle_lanes") + ": " + " · ".join(items)), road_width))
        roads.append([("━" * road_width, "edge")])
    age = max(0, (now - snapshot.captured_at).total_seconds())
    runs = {run["number"]: run for run in status["active_runs"]}
    table_width = (width - 2) * 3 // 5 if side_by_side else width
    events_width = width - table_width - 2 if side_by_side else width
    inner = table_width - 2
    lane_width = min(13, inner // 6)
    phase_width = min(11, inner // 6)
    activity_width = min(24, inner // 3)
    widths = [lane_width, inner - lane_width - phase_width - activity_width - 3, phase_width, activity_width]
    table = watch._columns([t("watch_lane"), t("watch_now"), t("watch_phase"), ""], widths, "bold")
    for _, row in busy:
        titles = {item["number"]: item.get("title") for item in row.get("running", [])}
        for number in lane_issues(row) or [row["issue"]]:
            run = runs.get(number)
            if number == row["issue"]:
                shown = row
            else:
                # A clone run: its own phase and title, judged as a working run.
                shown = {**row, "state": "working", "issue": number, "title": titles.get(number) or "",
                         "phase": run["phase"] if run else None,
                         "last_activity_seconds": _age(run.get("last_activity_at") or run.get("started_at"),
                                                       snapshot.captured_at) if run else None}
            phase = "" if shown["state"] == "blocked" else watch.text(shown["phase"] or shown["state"], language)
            task = f"#{number} {shown['title'] or ''}".rstrip() if number is not None else "—"
            if run and run["attempts"] > 1:
                task += " · " + t("watch_retry", count=run["attempts"])
            activity = watch._activity(shown, language, age, config.stall_minutes, run.get("pid") if run else None)
            table += watch._columns([lane_name(row), task, phase, activity], widths,
                              "yellow" if activity.startswith("○") or shown["state"] == "blocked" else "")
    if not busy:
        table.append(watch._line(t("watch_all_idle")))
    events: list[Line] = []
    for event in snapshot.events:
        when = watch.datetime.fromisoformat(event["at"]).astimezone().strftime("%H:%M") if event.get("at") else "—"
        number = f" #{event['issue']}" if event.get("issue") is not None else ""
        events.extend(watch._wrap(f"{when}{number} {event['summary']} ✓", events_width - 2))
    if not events:
        events.append(watch._line(t("watch_no_events")))
    work = watch._panel(table, table_width, t("watch_now"))
    handled = watch._panel(events, events_width, t("watch_events"))
    summary = t("summary", **summary_values(status["summary"]))
    if status.get("as_of"):
        observed = watch.datetime.fromisoformat(status["as_of"]).astimezone().strftime("%H:%M")
        summary += " · " + t("status_as_of", time=observed)
    first = [watch._line(t("status_unknown") + "  " + summary, "yellow")] if status_unknown(status) else []
    frame = (first or [watch._line(summary, "bold")]) + [[]]
    frame += watch._beside(left, roads, orchestrator_width, links=True) if side_by_side else left + roads
    frame += [[]] + (watch._beside(work, handled, table_width) if side_by_side else work + handled)
    if selected is not None:
        detail = watch.render_detail(config, selected, repo_root, done_count=len(snapshot.done.get(selected, [])))
        frame += [[]] + watch._panel([watch._line(line) for line in detail.splitlines()], width, t("watch_detail"))
    frame += [watch._line(t("watch_help_many" if len(rows) > 9 else "watch_help", count=len(rows)))]
    return [watch._fit(list(line), width) for line in frame]


def render_frame(
    snapshot: Snapshot, config: Config, repo_root: Path, *, tick: int = 0, width: int = 120,
    selected: str | None = None, now: datetime | None = None,
) -> str:
    """A single plain-text frame, sharing the live view's layout and data."""
    from foragent import watch

    return "\n".join("".join(value for value, style in line).rstrip() for line in watch._frame(
        snapshot, config, repo_root, tick=tick, width=max(40, width), selected=selected,
        now=now or snapshot.captured_at,
    ))
