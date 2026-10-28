"""The open issue box: one issue's facts from the local records, in place of the lane rows (#170)."""

from pathlib import Path
from typing import Any

from foragent.config import Config
from foragent.issue import parse_body
from foragent.state import StateStore
from foragent.status import issue_state, local_group
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 6
GAP = False  # The box stands where the lane rows were, under the header.
KEEP = True  # A person opened it; it stays when the screen is short.


def issue_url(config: Config, number: int) -> str | None:
    """The issue's GitHub page; without a configured repository there is no link."""
    repo = getattr(config, "repo", None)
    return f"https://github.com/{repo}/issues/{number}" if repo else None


def _latest_run(store: StateStore, number: int) -> Any:
    run = store.get_run(number)
    if run is not None:
        return run
    past = [item for item in store.read_history() if item.number == number]
    return max(past, key=lambda item: item.started_at or "") if past else None


def phase_word(store: StateStore, config: Config, number: int, row: dict[str, Any]) -> str:
    """The issue's row phase word; a finished issue is done, a queued one waits for a turn."""
    from foragent import watch
    from foragent.watch_sections.lanes import _kind

    run = _latest_run(store, number)
    # The issue, not its run, decides done: a merged run of a reopened issue is not done.
    if row.get("state") == "closed" or (not row and run is not None and run.outcome == "merged"):
        return watch.text("acorns_done", config.language)
    running = run is not None and run.status == "running"
    # The same rule as the row: labels, the running run and the recorded PR (an open
    # PR waits for a person).
    group = local_group({**row, "labels": row.get("labels") or []})
    state = issue_state(row.get("labels"), running, group) or "queued"
    # A run that ended keeps the phase it reached; a queued issue has none of its own.
    phase = run.phase if run is not None and state != "queued" else None
    return watch.text(_kind(state, phase)[1], config.language)


def issue_box(number: int, store: StateStore, config: Config, lane: str | None = None) -> list[str]:
    """The box's text lines, read only from the local issue and run records.

    ``lane`` is the lane of the row the box was opened from; it wins over the records.
    """
    from foragent import watch

    language = config.language
    row = next((item for item in store.local_issues() if item.get("number") == number), {})
    lines = [f"#{number} {watch._clean(row.get('title') or '')}".rstrip()]
    if (url := issue_url(config, number)) is not None:
        lines.append(url)
    if lane is None:
        # Without the row's lane: the latest run's lane, as the rows use it, then the label.
        run = _latest_run(store, number)
        labels = row.get("labels") or []
        lane = (run.lane if run is not None else None) or (
            watch.issue_lane(labels, config.lanes) if any(label.startswith("lane:") for label in labels) else None)
    if lane:
        lines.append(f"{watch.text('acorns_lane', language)}: {watch.lane_display(config, lane, language)}")
    lines.append(f"{watch.text('acorns_phase', language)}: {phase_word(store, config, number, row)}")
    parsed = parse_body(row.get("body") or "")
    if parsed.goal:
        lines += ["", watch.text("acorns_goal", language)] + ["  " + line for line in parsed.goal.splitlines()]
    if parsed.done_when_commands:
        lines += ["", watch.text("acorns_done_when", language)] + [
            "  " + line for command in parsed.done_when_commands for line in command.splitlines()]
    return lines


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    from foragent import watch

    state = context.get("state")
    number = None if state is None else state.get("issue")
    store = None if state is None else state.get("store")
    if number is None or store is None:
        return []
    width = context["width"]
    lines: list[Line] = []
    for text in watch.issue_box(number, store, config, state.get("issue_lanes", {}).get(number)):
        lines += watch._wrap(text, width - 2) if text else [[]]
    hits = context.get("hits")
    if hits is not None:
        # Clicking the number again closes the box.
        hits.append((1, 1, 1 + watch._cells(f"#{number}"), 2, ("acorn", number)))
    return watch._panel(lines, width, f"#{number}")
