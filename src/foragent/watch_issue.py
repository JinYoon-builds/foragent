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
# What each recorded CI read says, in the view's words.
READS = {"pass": "passed", "fail": "failed", "none": "no_checks", "not-started": "not_started", "pending": "pending"}


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


def recorded_ci(store: StateStore, number: int, run) -> str | None:
    """This run's newest recorded CI result, or None when the run recorded none.

    The runner keeps every CI read it acts on (``ci-read``: the CI wait, the final merge inputs,
    jobs that never started) and every CI code failure (``ci-failure``, also a comparison that
    fails a head GitHub passed). The newest wins; at equal times the one written last wins
    (the flow log keeps written order, and a comparison failure is written after the read it overrides).
    """
    found: list[tuple[datetime, int, str]] = []
    for index, record in enumerate(store.flow_records()):
        if record["issue"] != number or record["run_started_at"] != run.started_at:
            continue
        if (at := _at(record["at"])) is None:
            continue
        if record["kind"] == "ci-failure":
            found.append((at, index, "failed"))
        elif record["kind"] == "ci-read" and record["data"].get("status") in READS:
            found.append((at, index, READS[record["data"]["status"]]))
    return max(found, key=lambda item: item[:2])[2] if found else None


def ci_state(store: StateStore, number: int) -> str:
    """The CI line for the issue's newest run, from recorded facts only.

    A live run in its CI step: running. Otherwise the run's newest recorded CI read decides
    (a pending read means CI is still running while the run lives, and no result once it ended).
    Without a record: a live run has not started CI; a finished run, whatever step it stopped in,
    recorded no CI result. finish_run keeps the last step, so no step ever stands for a result.
    """
    run = latest_run(store, number)
    if run is None:
        return "none"
    live = run.status == "running"
    if live and run.phase == "ci":
        return "running"
    recorded = recorded_ci(store, number, run)
    if recorded == "pending" and live:
        return "running"
    if recorded not in {None, "pending"}:
        return recorded
    if run.outcome == "ci-failed":
        return "failed"  # the runner's own terminal fact: CI failed after every attempt
    return "none" if live else "unrecorded"


def _title(store: StateStore, number: int) -> str:
    row = next((row for row in store.local_issues() if row.get("number") == number), {})
    return str(row.get("title") or "")


def rows(snapshot: Snapshot, store: StateStore, config: Config, repo_root: Path, number: int, *,
         now: datetime, width: int) -> list[Line]:
    """The issue's rows: the acorn explanation (or its recorded state), last review, CI."""
    from foragent import watch
    from foragent.watch_sections import lane_bubble

    language = config.language
    run = latest_run(store, number)
    if run is not None and run.status != "running":
        # A finished run: what it ended with, never what its last step was doing.
        found = _ended(store, config, number, run)
    else:
        found = lane_bubble.explain(("acorn", number), snapshot, config, repo_root,
                                    now=now, width=width, state=None)
        if found is None:
            found = [[(watch._clean(f"#{number} {_title(store, number)}".rstrip()), "bold")]]
            if run is not None and run.phase:
                found.append([(text("issue_view_state", language, state=run.phase), "")])
    review = last_review(store, number)
    found.append([(text("issue_view_review", language, verdict=review[0], round=review[1]) if review
                   else text("issue_view_review_none", language), "")])
    found.append([(text(f"issue_view_ci_{ci_state(store, number)}", language), "")])
    return found


def _ended(store: StateStore, config: Config, number: int, run) -> list[Line]:
    """Title, lane, the recorded outcome and its recorded detail, for a finished run."""
    from foragent import watch
    from foragent.i18n import MESSAGES
    from foragent.labels import lane_display

    language = config.language
    key = f"issue_view_outcome_{run.outcome}"
    word = text(key, language) if key in MESSAGES.get(language, MESSAGES["en"]) else (run.outcome or run.status)
    found = [[(watch._clean(f"#{number} {_title(store, number)}".rstrip()), "bold")]]
    if run.lane:
        found.append([(text("issue_view_lane", language, lane=lane_display(config, run.lane, language)), "")])
    found.append([(text("issue_view_ended", language, outcome=word), "")])
    detail = " ".join(run.detail.split())
    if detail and detail != f"Run finished with outcome {run.outcome}.":  # finish_run's default repeats the outcome
        found.append([(watch._clean(detail), "")])
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
