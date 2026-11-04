"""The dashboard focused on one issue: the acorn explanation plus its last review and CI state.

Reads local facts only (the status snapshot, the journal, the flow log); writes nothing.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import re
import subprocess
import sys

from foragent.config import Config, ConfigError
from foragent.i18n import text
from foragent.state import StateStore
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

BRANCH = re.compile(r"foragent/issue-(\d+)")
# What a CI segment closes into when CI passed: a merge step, or the run's merge outcome.
AFTER_CI = frozenset({"merging", "merge-lock-wait", "merge-turn-wait", "merged", "merge-queued"})


def infer(folder: Path) -> int | None:
    """The issue whose lane branch is checked out in ``folder``, if any."""
    try:
        branch = subprocess.run(["git", "-C", str(folder), "symbolic-ref", "--quiet", "--short", "HEAD"],
                                capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        return None
    return int(match.group(1)) if (match := BRANCH.fullmatch(branch)) else None


def known(store: StateStore, number: int) -> bool:
    """Foragent has a local record of the issue: its cached row or one of its runs."""
    if any(row.get("number") == number for row in store.local_issues()):
        return True
    return store.get_run(number) is not None or any(run.number == number for run in store.read_history())


def last_review(store: StateStore, number: int) -> tuple[str, int] | None:
    """The newest reviewer verdict for the issue and its round, in journal order."""
    from foragent import orch_events
    from foragent.watch_lane_facts import _reviews

    try:
        found = _reviews(orch_events.read_events(store)).get(number)
    except OSError:
        return None
    return (found[-1][2], found[-1][1]) if found else None


def latest_run(store: StateStore, number: int):
    """The issue's newest run: its run record, else its newest history entry."""
    run = store.get_run(number)
    if run is not None:
        return run
    return next((run for run in reversed(store.read_history()) if run.number == number), None)


def _at(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return moment if moment is not None and moment.tzinfo is not None else None


def _ci_passes(store: StateStore, number: int, since: datetime) -> list[datetime]:
    """When this run's CI segments closed into a merge step: the recorded move past CI.

    The phase journal writes one ``phase`` event per closed segment, ``ci -> <next step
    or run outcome>``; a step name alone never says CI ran in this run.
    """
    from foragent import orch_events

    try:
        events = orch_events.read_events(store)
    except OSError:
        return []
    found = []
    for event in events:
        opened, closed = _at(event.get("phase_started_at")), _at(event.get("at"))
        summary = event.get("summary")
        if (event.get("kind") == "phase" and event.get("issue") == number and event.get("phase") == "ci"
                and opened is not None and closed is not None and opened >= since
                and isinstance(summary, str) and summary.rpartition(" -> ")[2] in AFTER_CI):
            found.append(closed)
    return found


def ci_state(store: StateStore, number: int) -> str:
    """running, failed, passed, stopped or none, for the issue's newest run.

    Only recorded facts of this run count: its live step, its outcome, its CI failure
    records and its journaled CI segments. finish_run keeps the last phase, and a resumed
    run can reach a merge or conflict step without CI, so a step name is never a CI result.
    """
    run = latest_run(store, number)
    started = _at(run.started_at) if run is not None else None
    if run is None or started is None:
        return "none"
    live = run.status == "running"
    if live and run.phase == "ci":
        return "running"
    if run.outcome == "ci-failed":
        return "failed"
    if run.outcome == "merge-queued":
        return "passed"  # The merge owner admits a run only after its CI passed.
    failures = [at for record in store.flow_records()
                if record["kind"] == "ci-failure" and record["issue"] == number
                and record["run_started_at"] == run.started_at and (at := _at(record["at"])) is not None]
    passes = _ci_passes(store, number, started)
    if passes and (not failures or max(passes) > max(failures)):
        return "passed"  # A later CI attempt of this run passed after an earlier failure.
    if failures:
        return "failed"
    if not live and run.phase == "ci":
        return "stopped"  # The run ended (timeout, stop, crash) before CI gave a result.
    return "none"


def _title(store: StateStore, number: int) -> str:
    row = next((row for row in store.local_issues() if row.get("number") == number), {})
    return str(row.get("title") or "")


def rows(snapshot: Snapshot, store: StateStore, config: Config, repo_root: Path, number: int, *,
         now: datetime, width: int) -> list[Line]:
    """The issue's rows: the acorn explanation (or its recorded state), last review, CI."""
    from foragent import watch
    from foragent.watch_sections import lane_bubble

    language = config.language
    found = lane_bubble.explain(("acorn", number), snapshot, config, repo_root,
                                now=now, width=width, state=None)
    if found is None:
        run = latest_run(store, number)
        found = [[(watch._clean(f"#{number} {_title(store, number)}".rstrip()), "bold")]]
        if run is not None:
            state = run.phase if run.status == "running" and run.phase else (run.outcome or run.status)
            found.append([(text("issue_view_state", language, state=state), "")])
    review = last_review(store, number)
    found.append([(text("issue_view_review", language, verdict=review[0], round=review[1]) if review
                   else text("issue_view_review_none", language), "")])
    found.append([(text(f"issue_view_ci_{ci_state(store, number)}", language), "")])
    return found


def plain(lines: list[Line]) -> str:
    return "\n".join("".join(value for value, _style in line).rstrip() for line in lines)


def run(read: Callable[[], Snapshot], store: StateStore, config: Config, repo_root: Path, number: int, *,
        once: bool, now: Callable[[], datetime]) -> int:
    if not known(store, number):
        print(text("issue_view_unknown", config.language, number=number), file=sys.stderr)
        return 1
    import shutil

    if once:
        width = shutil.get_terminal_size((120, 24)).columns
        print(plain(rows(read(), store, config, repo_root, number, now=now(), width=width)))
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ConfigError(text("watch_terminal", config.language))
    import curses
    import threading

    latest: list[Snapshot] = []
    stop = threading.Event()

    def refresh() -> None:
        # Reading the snapshot can take seconds; the screen keeps answering keys meanwhile.
        while not stop.is_set():
            try:
                latest[:] = [read()]
            except Exception:
                pass  # Keep the last snapshot; the next round tries again.
            stop.wait(5)

    def draw(screen) -> None:
        curses.curs_set(0)
        screen.timeout(500)
        drawn, lines = None, []
        while True:
            height, width = screen.getmaxyx()
            shown = (latest[0] if latest else None, width)
            if shown != drawn:
                # Rows are rebuilt only when a new snapshot arrives or the terminal resizes.
                drawn = shown
                lines = (rows(shown[0], store, config, repo_root, number, now=now(), width=max(1, width - 1))
                         if shown[0] is not None else [[(f"#{number} …", "")]])
                lines += [[("", "")], [(text("issue_view_quit", config.language), "")]]
            screen.erase()
            for y, line in enumerate(lines[:height]):
                try:
                    screen.addnstr(y, 0, "".join(value for value, _ in line), max(1, width - 1))
                except curses.error:
                    pass
            screen.refresh()
            if screen.getch() in (ord("q"), ord("Q")):
                return

    threading.Thread(target=refresh, daemon=True).start()
    try:
        curses.wrapper(draw)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0
