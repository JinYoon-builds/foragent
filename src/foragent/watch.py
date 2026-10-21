"""Read-only lane conveyor, with a plain frame and a curses terminal view."""

from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice, zip_longest
import json
from pathlib import Path
import sys
import time
import unicodedata
from typing import Any

from foragent.config import Config, ConfigError
from foragent.i18n import text
from foragent.labels import issue_lane, lane_display
from foragent.state import StateStore
from foragent.tracker import Issue, Tracker


# Only the pickaxe moves. One hit every four 0.3-second frames.
PICK = ("  ⛏ ", " ⛏  ", "⛏✦  ", " ⛏  ")
Span = tuple[str, str]
Line = list[Span]


@dataclass
class Snapshot:
    status: dict[str, Any]
    done: dict[str, list[Issue]]
    order: Issue | None
    completed: int
    total: int
    eta: datetime | None
    events: list[dict[str, Any]]
    captured_at: datetime


def recent_events(store: StateStore) -> list[dict[str, Any]]:
    """Ignore an incomplete append without taking the writer's lock."""
    events: deque[dict[str, Any]] = deque(maxlen=5)
    try:
        with (store.root / "orch-events.jsonl").open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and isinstance(event.get("summary"), str):
                    events.append(event)
    except FileNotFoundError:
        pass
    return list(reversed(events))


def collect(
    status: dict[str, Any], tracker: Tracker, store: StateStore, config: Config, now: datetime,
) -> Snapshot:
    """Supplement the shared status JSON with native order edges and completions."""
    issues = {issue.number: issue for issue in tracker.list_issues()}
    rows = status["lanes"]
    # Prefer the order currently executing, then other visible work. Never add
    # progress from a different order into this order's denominator.
    candidates = [run["number"] for run in status["active_runs"]]
    candidates += [row["issue"] for row in rows.values() if row["issue"] is not None]
    candidates += [issue.number for issue in issues.values() if issue.state == "open"]
    order = None
    for number in dict.fromkeys(candidates):
        parent = tracker.parent_of(number)
        if parent is not None:
            candidate = issues.get(parent) or tracker.get_issue(parent)
            if candidate.state == "open":
                order = candidate
                break
    children = tracker.sub_issues(order.number) if order else []
    if order is None:
        # An order can remain open after its last child has finished.
        for candidate in sorted(issues.values(), key=lambda issue: issue.number):
            if candidate.state == "open" and (children := tracker.sub_issues(candidate.number)):
                order = candidate
                break
    child_numbers = {issue.number for issue in children}
    completed = sum(issue.state == "closed" for issue in children)
    history = store.read_history()  # status.history deliberately contains only ten entries
    merged = {}
    for run in history:
        if run.outcome == "merged":
            merged.pop(run.number, None)
            merged[run.number] = run
    done: dict[str, list[Issue]] = {}
    durations = []
    for number, run in merged.items():
        if order is not None and number not in child_numbers:
            continue
        issue = issues.get(number)
        if issue is None:
            continue
        done.setdefault(run.lane, []).append(issue)
        duration = (datetime.fromisoformat(run.last_progress_at) - datetime.fromisoformat(run.started_at)).total_seconds()
        if duration > 0:
            durations.append(duration)
    eta = None
    if order is not None and durations and completed < len(children):
        # A lane is serial; max_parallel also caps total throughput. Historical
        # run duration is an estimate, so the UI explicitly prefixes the time ~.
        workloads = Counter(issue_lane(issue.labels, config.lanes) for issue in children if issue.state == "open")
        batches = max(max(workloads.values()), sum(workloads.values()) / config.max_parallel)
        eta = now + timedelta(seconds=batches * sum(durations) / len(durations))
    return Snapshot(status, done, order, completed, len(children), eta, recent_events(store), now)


def _clean(value: Any) -> str:
    return " ".join("".join(c if c in "\n\r\t" else "" if unicodedata.category(c).startswith("C") else c
                            for c in str(value)).split())


def render_detail(config: Config, slug: str, repo_root: Path, done_count: int = 0) -> str:
    """Show lane configuration and exactly the first three memory lines."""
    language = config.language
    lane = config.lanes.get(slug, {})
    executor = lane.get("executor") or config.executor
    model = lane.get("model") or config.executor_model or text("watch_default_model", language)
    none = text("watch_none", language)
    lines = [
        f"{text('watch_lane', language)}: {lane_display(config, slug, language)} ({slug})",
        f"{text('watch_paths', language)}: {', '.join(lane.get('paths', [])) or none}",
        f"{text('watch_executor', language)}: {executor}",
        f"{text('watch_model', language)}: {model}",
        f"{text('watch_skills', language)}: {', '.join(lane.get('skills', [])) or none}",
        text("watch_done_count", language, count=done_count),
        text("watch_memory", language) + ":",
    ]
    agent = lane.get("agent") or f"fa-{slug}"
    memory = Path(repo_root) / ".claude" / "agent-memory" / agent / "MEMORY.md"
    try:
        with memory.open(encoding="utf-8", errors="replace") as stream:
            lessons = [line.rstrip("\n") for line in islice(stream, 3)]
    except FileNotFoundError:
        lessons = []
    lines.extend(lessons or [text("watch_no_memory", language)])
    return "\n".join(_clean(line) for line in lines)


def _cells(value: str) -> int:
    return sum(0 if unicodedata.combining(c) or unicodedata.category(c) in {"Mn", "Me", "Cf"}
               else 2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in value)


def _fit(line: Line, width: int) -> Line:
    """Clip and pad in terminal cells, preserving the color spans."""
    result: Line = []
    remaining = max(0, width)
    for value, style in line:
        clipped = ""
        for char in value:
            size = _cells(char)
            if size > remaining:
                break
            clipped += char
            remaining -= size
        result.append((clipped, style))
        if remaining == 0 or len(clipped) < len(value):
            break
    if remaining:
        result.append((" " * remaining, "road"))
    return result


def _line(value: Any, style: str = "") -> Line:
    return [(_clean(value), style)]


def _wrap(value: str, width: int, style: str = "") -> list[Line]:
    lines: list[Line] = []
    current = ""
    for word in _clean(value).split():
        if current and _cells(current + " " + word) > width:
            lines.append([(current, style)])
            current = ""
        while _cells(word) > width:
            piece = _fit([(word, style)], width)[0][0]
            lines.append([(piece, style)])
            word = word[len(piece):]
        current += (" " if current else "") + word
    return lines + [[(current, style)]] if current or not lines else lines


def _columns(values: list[str], widths: list[int], style: str = "") -> list[Line]:
    columns = [_wrap(value, width, style) for value, width in zip(values, widths)]
    result = []
    for cells in zip_longest(*columns, fillvalue=[]):
        row: Line = []
        for index, (cell, width) in enumerate(zip(cells, widths)):
            if index:
                row.append((" ", ""))
            row.extend(_fit(cell, width))
        result.append(row)
    return result


def _panel(lines: list[Line], width: int, title: str) -> list[Line]:
    inside = width - 2
    heading = _fit(_line(f" {title} ", "bold"), inside)
    return [[("┌", "edge"), *heading, ("┐", "edge")],
            *[[("│", "edge"), *_fit(line, inside), ("│", "edge")] for line in lines],
            [("└" + "─" * inside + "┘", "edge")]]


def _beside(left: list[Line], right: list[Line], left_width: int, *, links: bool = False) -> list[Line]:
    return [(*_fit(a, left_width), ("▶ " if links and index % 3 == 1 else "  ", "blue"), *b)
            for index, (a, b) in enumerate(zip_longest(left, right, fillvalue=[]))]


def _activity(row: dict[str, Any], language: str, age: float) -> str:
    if row["state"] == "blocked":
        return "● " + text("blocked", language)
    if row["state"] != "working":
        return ""
    seconds = row["last_activity_seconds"]
    if seconds is not None and seconds + age > 600:
        return text("watch_stalled", language, minutes=int((seconds + age) // 60))
    return "●"


def _frame(
    snapshot: Snapshot, config: Config, repo_root: Path, *, tick: int, width: int,
    selected: str | None, now: datetime,
) -> list[Line]:
    language = config.language
    status = snapshot.status
    rows = list(status["lanes"].values())
    t = lambda key, **values: text(key, language, **values)
    side_by_side = width >= 100
    orchestrator_width = 28 if side_by_side else width
    road_width = width - orchestrator_width - 2 if side_by_side else width
    state = "watch_handling" if status["summary"]["blocked"] else "watch_monitoring"
    overview = [_line(t(state), "yellow" if status["summary"]["blocked"] else "green")]
    if snapshot.order:
        filled = round(10 * snapshot.completed / snapshot.total) if snapshot.total else 0
        overview += [_line(t("watch_order") + f" #{snapshot.order.number}"),
                     _line(snapshot.order.title),
                     _line("▓" * filled + "░" * (10 - filled) + f" {snapshot.completed}/{snapshot.total}", "green")]
    else:
        overview.append(_line(t("watch_no_order")))
    overview.append(_line(t("watch_waiting", count=status["summary"]["waiting_human"]), "bold"))
    eta = (t("watch_complete") if snapshot.order and snapshot.completed == snapshot.total
           else t("watch_eta", time="~" + snapshot.eta.astimezone().strftime("%H:%M") if snapshot.eta
                  else t("watch_estimating") if snapshot.order else "—"))
    overview.append(_line(eta))
    left = _panel(overview, orchestrator_width, "🧭 " + t("watch_orchestrator"))
    roads: list[Line] = [[("━" * road_width, "edge")]]
    for index, row in enumerate(rows, 1):
        slug, state = row["slug"], row["state"]
        lane = config.lanes.get(slug, {})
        executor = lane.get("executor") or config.executor
        color, badge = ("claude", "✳") if executor == "claude" else ("codex", "✺")
        display = row["lane_display"]
        # The lane title (without its decorative emoji) names its responsible agent.
        title = lane.get("title") or lane_display(config, slug, language).removeprefix("🧰 ")
        prefix = _fit(_line(f"{index} {display} ▸ ", "bold"), min(19, road_width // 3))
        queue = row["queued"]
        label_width = min(25, max(8, road_width // 3))
        agent_label = "".join(value for value, style in _fit(_line(t('watch_agent', lane=title)), label_width)).rstrip()
        label = f"▐ {badge} {agent_label} ▌"
        current = row["issue"] if state in {"working", "blocked", "waiting-human"} else None
        tool = PICK[tick % 4] if state == "working" else " ✋ " if state == "waiting-human" else " ●  " if state == "blocked" else " zZ "
        tail: Line = [(f" #{current} ", "hit" if state == "working" and tick % 4 == 2 else color)] if current is not None else []
        tail += [(tool, "yellow"), (label, color)]
        space = max(0, road_width - sum(_cells(value) for value, style in prefix + tail))
        shown = len(queue)
        while True:
            road: Line = []
            if shown < len(queue):
                road.append((f"+{len(queue) - shown} ", ""))
            for item in reversed(queue[:shown]):
                road += [(f" #{item['number']} ", "queue"), (" ", "road")]
            road_cells = sum(_cells(value) for value, style in road)
            if road_cells <= space or shown == 0:
                break
            shown -= 1
        roads.append(_fit([*prefix, (" " * max(0, space - road_cells), "road"), *road, *tail], road_width))
        done = snapshot.done.get(slug, [])
        roads.append(_fit(_line("  " + t("watch_done", count=len(done), title=done[-1].title if done else "—"), "green"), road_width))
        edge = "━" * road_width if index == len(rows) else ("▬▬▬   " * (road_width // 6 + 1))[:road_width]
        roads.append([(edge, "edge" if index == len(rows) else "divider")])
    age = max(0, (now - snapshot.captured_at).total_seconds())
    runs = {run["number"]: run for run in status["active_runs"]}
    table_width = (width - 2) * 3 // 5 if side_by_side else width
    events_width = width - table_width - 2 if side_by_side else width
    inner = table_width - 2
    lane_width = min(13, inner // 6)
    phase_width = min(11, inner // 6)
    activity_width = min(24, inner // 3)
    widths = [lane_width, inner - lane_width - phase_width - activity_width - 3, phase_width, activity_width]
    table = _columns([t("watch_lane"), t("watch_now"), t("watch_phase"), ""], widths, "bold")
    for row in rows:
        phase = "" if row["state"] == "blocked" else text(row["phase"] or row["state"], language)
        task = f"#{row['issue']} {row['title']}" if row["issue"] is not None else "—"
        run = runs.get(row["issue"])
        if run and run["attempts"] > 1:
            task += " · " + t("watch_retry", count=run["attempts"])
        activity = _activity(row, language, age)
        table += _columns([row["lane_display"], task, phase, activity], widths,
                          "yellow" if activity.startswith("○") or row["state"] == "blocked" else "")
    events: list[Line] = []
    for event in snapshot.events:
        when = datetime.fromisoformat(event["at"]).astimezone().strftime("%H:%M") if event.get("at") else "—"
        number = f" #{event['issue']}" if event.get("issue") is not None else ""
        events.extend(_wrap(f"{when}{number} {event['summary']} ✓", events_width - 2))
    if not events:
        events.append(_line(t("watch_no_events")))
    work = _panel(table, table_width, t("watch_now"))
    handled = _panel(events, events_width, t("watch_events"))
    frame = [_line(t("summary", **status["summary"]), "bold"), []]
    frame += _beside(left, roads, orchestrator_width, links=True) if side_by_side else left + roads
    frame += [[]] + (_beside(work, handled, table_width) if side_by_side else work + handled)
    if selected is not None:
        detail = render_detail(config, selected, repo_root, done_count=len(snapshot.done.get(selected, [])))
        frame += [[]] + _panel([_line(line) for line in detail.splitlines()], width, t("watch_detail"))
    frame += [_line(t("watch_help_many" if len(rows) > 9 else "watch_help", count=len(rows)))]
    return [_fit(list(line), width) for line in frame]


def render_frame(
    snapshot: Snapshot, config: Config, repo_root: Path, *, tick: int = 0, width: int = 120,
    selected: str | None = None, now: datetime | None = None,
) -> str:
    """A single plain-text frame, sharing the live view's layout and data."""
    return "\n".join("".join(value for value, style in line).rstrip() for line in _frame(
        snapshot, config, repo_root, tick=tick, width=max(40, width), selected=selected,
        now=now or snapshot.captured_at,
    ))


def _terminal(screen, refresh: Callable[[], Snapshot], snapshot: Snapshot, config: Config, repo_root: Path, now):
    import curses

    styles = {"bold": curses.A_BOLD, "edge": curses.A_BOLD}
    if curses.has_colors():
        curses.start_color()
        # Asphalt background; white queue blocks and fixed yellow lane dividers.
        asphalt = 236 if curses.COLORS >= 256 else curses.COLOR_BLACK
        orange = 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW
        palette = [(curses.COLOR_WHITE, asphalt), (curses.COLOR_YELLOW, asphalt),
                   (curses.COLOR_GREEN, asphalt), (curses.COLOR_BLACK, curses.COLOR_WHITE),
                   (curses.COLOR_BLACK, orange), (curses.COLOR_BLACK, curses.COLOR_GREEN),
                   (curses.COLOR_BLUE, asphalt)]
        for index, (foreground, background) in enumerate(palette, 1):
            curses.init_pair(index, foreground, background)
        styles.update(road=curses.color_pair(1), edge=curses.color_pair(1) | curses.A_BOLD,
                      divider=curses.color_pair(2), yellow=curses.color_pair(2),
                      green=curses.color_pair(3), queue=curses.color_pair(4),
                      hit=curses.color_pair(4) | curses.A_BOLD, claude=curses.color_pair(5),
                      codex=curses.color_pair(6), blue=curses.color_pair(7))
        screen.bkgd(" ", styles["road"])
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(300)
    selected = None
    show_detail = False
    offset = 0
    digits = ""
    error = ""
    animation_started = time.monotonic()
    next_refresh = animation_started + 5
    pending = None
    with ThreadPoolExecutor(max_workers=1) as worker:
        while True:
            if pending is not None and pending.done():
                try:
                    snapshot = pending.result()
                    error = ""
                except Exception as exc:
                    error = text("watch_refresh_failed", config.language, error=_clean(exc))
                pending = None
                next_refresh = time.monotonic() + 5
            if pending is None and time.monotonic() >= next_refresh:
                pending = worker.submit(refresh)
            tick = int((time.monotonic() - animation_started) / 0.3)
            height, width = screen.getmaxyx()
            frame = _frame(snapshot, config, repo_root, tick=tick, width=max(40, width - 1), selected=selected, now=now())
            # Keep keyboard help on screen even when there are more lanes than rows.
            body, footer = frame[:-1], frame[-1]
            if error:
                footer = _line(error, "yellow")
            if show_detail:
                offset = len(body)
                show_detail = False
            offset = max(0, min(offset, len(body) - max(1, height - 1)))
            visible = body[offset:offset + max(0, height - 1)]
            screen.erase()
            for y, line in [*enumerate(visible), (height - 1, footer)]:
                x = 0
                for value, style in _fit(line, max(0, width - 1)):
                    try:
                        screen.addstr(y, x, value, styles.get(style, 0))
                    except curses.error:
                        pass  # A resize may occur between measuring and painting.
                    x += _cells(value)
            screen.refresh()
            key = screen.getch()
            slugs = list(snapshot.status["lanes"])
            if key in (ord("q"), ord("Q"), 27):
                return
            if key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                offset += 1 if key == curses.KEY_DOWN else max(1, height - 2)
            elif key in (curses.KEY_UP, curses.KEY_PPAGE):
                offset -= 1 if key == curses.KEY_UP else max(1, height - 2)
            elif key in range(ord("0"), ord("9") + 1):
                digits = (digits + chr(key))[-len(str(len(slugs))):]
                if len(slugs) > 9:
                    continue
            elif key not in (10, 13, curses.KEY_ENTER):
                continue
            if digits:
                number = int(digits)
                digits = ""
                if 1 <= number <= len(slugs):
                    slug = slugs[number - 1]
                    selected = None if selected == slug else slug
                    show_detail = selected is not None
                    if selected is None:
                        offset = 0


def run(
    status: Callable[[], dict[str, Any]], tracker: Tracker, store: StateStore,
    config: Config, repo_root: Path, *, once: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    if not once and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ConfigError(text("watch_terminal", config.language))
    refresh = lambda: collect(status(), tracker, store, config, now())
    snapshot = refresh()
    if once:
        print(render_frame(snapshot, config, repo_root))
        return
    import curses

    try:
        curses.wrapper(_terminal, refresh, snapshot, config, repo_root, now)
    except KeyboardInterrupt:
        pass
    except curses.error as exc:
        raise ConfigError(str(exc)) from exc
