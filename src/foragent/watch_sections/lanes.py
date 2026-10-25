"""The orchestrator panel beside the lane roads, one road per lane with work."""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.status import summary_values
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot
from foragent.watch_layout import _clones, _split, _widths, _worker_rows

ORDER = 30


def _overview(snapshot: Snapshot, language: str) -> list[Line]:
    from foragent import watch

    status = snapshot.status
    t = lambda key, **values: watch.text(key, language, **values)
    state = "watch_handling" if status["summary"]["blocked"] else "watch_monitoring"
    overview = [watch._line(t(state), "yellow" if status["summary"]["blocked"] else "green")]
    if snapshot.order:
        # The combined bar and finish time live in the progress section.
        overview += [watch._line(t("watch_order") + f" #{snapshot.order.number}"),
                     watch._line(snapshot.order.title)]
    else:
        overview.append(watch._line(t("watch_no_order")))
    overview.append(watch._line(t("watch_waiting", **summary_values({"count": status["summary"]["waiting_human"]})), "bold"))
    if snapshot.requests:
        count = lambda name: sum(row.get("status") == name for row in snapshot.requests)
        overview.append(watch._line(t("watch_requests", processing=count("processing"), done=count("done"))))
    return overview


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    language, tick, top = config.language, context["tick"], context.get("top", 0)
    t = lambda key, **values: watch.text(key, language, **values)
    side_by_side, orchestrator_width, road_width, _, _ = _widths(context["width"])
    busy, idle = _split(snapshot.status)
    clones = _clones(snapshot.status)
    left = watch._panel(_overview(snapshot, language), orchestrator_width, "🧭 " + t("watch_orchestrator"))
    roads: list[Line] = [[("━" * road_width, "edge")]]
    for index, row in busy:
        slug, state = row["slug"], row["state"]
        lane = config.lanes.get(slug, {})
        executor = lane.get("executor") or config.executor
        color, badge = ("claude", "✳") if executor == "claude" else ("codex", "✺")
        # The lane title (without its decorative emoji) names its responsible agent.
        title = lane.get("title") or watch.lane_display(config, slug, language).removeprefix("🧰 ")
        label_width = min(25, max(8, road_width // 3))
        agent_label = "".join(value for value, style in watch._fit(watch._line(t('watch_agent', lane=title)), label_width)).rstrip()
        label = f"▐ {badge} {agent_label} ▌"
        tool = watch.PICK[tick % 4] if state == "working" else " ✋ " if state == "waiting-human" else " ●  " if state == "blocked" else " zZ "
        hit = "hit" if state == "working" and tick % 4 == 2 else color
        # Several workers get a road row each; the queue and done line stay on the first.
        for position, (display, numbers) in enumerate(_worker_rows(row, clones)):
            prefix = watch._fit(watch._line(f"{index} {display} ▸ ", "bold"), min(19, road_width // 3))
            queue = row["queued"] if position == 0 else []
            tail: Line = [(f" #{number} ", hit) for number in numbers]
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
            if position == 0:
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
    context.get("hits", []).append((0, top, orchestrator_width, top + len(left), ("orchestrator", None)))
    return watch._beside(left, roads, orchestrator_width, links=True) if side_by_side else left + roads
