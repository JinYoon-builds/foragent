"""The dashboard focused on one issue: the acorn explanation plus its last review verdict.

Reads local facts only (the status snapshot, run records, the journal); writes nothing.
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


def _issue(store: StateStore, number: int) -> dict:
    return next((row for row in store.local_issues() if row.get("number") == number), {})


def _title(store: StateStore, number: int) -> str:
    return str(_issue(store, number).get("title") or "")


def completion(issue: dict) -> str | None:
    """A closed issue's recorded completion: merged when the gate recorded its PR merged, else closed.

    Closure alone is never read as a merge; None while the issue is not recorded closed.
    """
    if issue.get("state") != "closed":
        return None
    return "merged" if issue.get("merged") or issue.get("merged_at") else "closed"


def current_state(snapshot: Snapshot | None, store: StateStore, number: int) -> str | None:
    """What the issue is doing now, read where the main dashboard reads it.

    A closed issue reads its recorded completion (merged or closed) first; open issues take
    their state from the status lane rows (working, queued, held). None: no list shows it.
    """
    done = completion(_issue(store, number))
    if done is not None:
        return done  # Recorded completion outranks a stale run any lane still lists.
    lanes = ((snapshot.status if snapshot is not None else None) or {}).get("lanes") or {}
    for row in lanes.values() if isinstance(lanes, dict) else lanes:
        if row.get("issue") == number and row.get("state") not in (None, "idle"):
            return row["state"]  # The lane's lead issue: its row state, as the main view draws it.
        for key, state in (("running", "working"), ("queued", "queued")):
            if any(_number(item) == number for item in row.get(key) or []):
                return state
        for item in row.get("held") or []:
            if _number(item) == number:
                return item.get("state") if isinstance(item, dict) and item.get("state") else "waiting-human"
    return None


# Every state current_state returns: status.collect_lanes (working, queued, waiting-human,
# blocked), watch_data.merge_turns (waiting-merge), the local row (merged, closed), none.
NOW_STATES = ("working", "queued", "waiting-human", "waiting-merge", "blocked", "merged", "closed", "none")


def now_line(state: str | None, language: str) -> str:
    """The state line; a state no producer named yet reads as the plain state, never a crash."""
    state = state or "none"
    if state in NOW_STATES:
        return text(f"issue_view_now_{state}", language)
    return text("issue_view_state", language, state=state)


def _number(item) -> int | None:
    return item.get("number") if isinstance(item, dict) else item


def rows(snapshot: Snapshot, store: StateStore, config: Config, repo_root: Path, number: int, *,
         now: datetime, width: int) -> list[Line]:
    """The issue's rows: the acorn explanation (or what a finished run ended with), last review."""
    from foragent import watch
    from foragent.watch_sections import lane_bubble

    from foragent.board_parents import replacements

    language = config.language
    run = latest_run(store, number)
    replaced_by = replacements(store, number)  # `fa orch close --superseded-by` closed it.
    issue = _issue(store, number)
    done = completion(issue)
    if done is not None:
        # Recorded completion leads, run or no run; any run below it is history.
        found = [[(watch._clean(f"#{number} {_title(store, number)}".rstrip()), "bold")],
                 [(now_line(done, language), "")]]
        if issue.get("pr_number"):
            found.append([(text(f"issue_view_pr_{done}", language, pr=issue["pr_number"]), "")])
        if run is not None and run.status != "running":
            found += _ended(store, config, number, run, stale=bool(replaced_by))[1:]
        elif run is not None and run.phase:
            found.append([(text("issue_view_stale_step", language, step=run.phase), "")])
    elif run is not None and run.status != "running":
        # A finished run is history: the issue's state now leads, then what the run ended with.
        found = _ended(store, config, number, run, stale=bool(replaced_by))
        found.insert(1, [(now_line(current_state(snapshot, store, number), language), "")])
    elif replaced_by:
        found = [[(watch._clean(f"#{number} {_title(store, number)}".rstrip()), "bold")]]
    else:
        found = lane_bubble.explain(("acorn", number), snapshot, config, repo_root,
                                    now=now, width=width, state=None)
        if found is None:
            found = [[(watch._clean(f"#{number} {_title(store, number)}".rstrip()), "bold")]]
            if run is not None and run.phase:
                found.append([(text("issue_view_state", language, state=run.phase), "")])
    if replaced_by:
        found.append([(text("issue_view_replaced", language,
                             issues=", ".join(f"#{n}" for n in replaced_by)), "")])
    review = last_review(store, number)
    found.append([(text("issue_view_review", language, verdict=review[0], round=review[1]) if review
                   else text("issue_view_review_none", language), "")])
    return fit(found, watch.issue_url(config, number), width)


def fit(lines: list[Line], url: str | None, width: int) -> list[Line]:
    """Every row within ``width`` terminal cells: a longer row wraps at spaces (a title, a lane's
    path list, a recorded detail stay whole), and the issue link prints whole or not at all,
    since a cut link is a different, wrong URL."""
    from foragent import watch

    fitted: list[Line] = []
    for line in lines:
        if any(style == "issue-link" for _value, style in line):
            if url and watch._cells(url) <= width:
                fitted.append([(url, "issue-link")])
            continue
        value = "".join(value for value, _style in line)
        fitted += watch._wrap(value, width) if watch._cells(value) > width else [line]
    return fitted


def _ended(store: StateStore, config: Config, number: int, run, *, stale: bool = False) -> list[Line]:
    """Title, lane, the recorded outcome and its recorded detail, for a finished run.

    ``stale``: the issue was since closed as replaced, so the run's next action no longer applies.
    """
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
    if detail and not stale and detail != f"Run finished with outcome {run.outcome}.":  # finish_run's default repeats the outcome
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
