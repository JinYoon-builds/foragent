"""Whose turn each open question awaits (#1160), from local facts only.

One projection for the lane view and the statusline, so both name the same
recipient. It reads the local issue records, the question inbox, spec-question
flags and run records; it never reads question wording and stores nothing.

- ``person``: an open inbox question lists the issue, or the issue carries
  needs-human and no answer came after it was routed. Routed questions are the
  person's (D71). Work parked for a person (waiting-person) is not a question.
  An open inbox question that lists no issue is the person's too; it has no row,
  so it is counted apart (``Questions.unlinked``).
- ``orchestrator``: a spec question not yet routed: the issue's flag says
  spec-question and its latest run ended with that outcome (a later run start, a
  retry or a merge, ends it). An inbox question about the issue answered at or
  after the flag also ends it, since the answer wins over the older flag.
- ``answered``: that spec or needs-human question was answered; the issue waits
  to restart. The needs-human label may stay until someone acts on the answer, so
  it is the person's again only when a needs-human marker comment is newer than
  every answer about the issue.

"At or after the flag" compares the answer with the earlier of the flag's own time
and when its file was written, so two writers' disagreeing clocks let the answer win;
a flag saved again after the answer (a new spec question) still names the orchestrator.
"""

from datetime import UTC, datetime
import re
from typing import Any, Iterable, NamedTuple

PERSON = "person"
ORCHESTRATOR = "orchestrator"
ANSWERED = "answered"
NEEDS_HUMAN = "needs-human"
SPEC = "spec-question"
# The machine marker on the first line of a comment that routes an issue to the person.
ROUTED = re.compile(r"<!-- foragent:needs-human reason=[a-z-]+ -->")


class Questions(NamedTuple):
    issues: dict[int, str]  # Issue number -> PERSON, ORCHESTRATOR or ANSWERED.
    unlinked: int  # Open inbox questions that list no issue: the person's.


def _time(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _latest_run(store, number: int, history: Iterable[Any] | None):
    """The issue's latest run: its run record, else its latest history entry."""
    run = store.get_run(number)
    if run is not None:
        return run
    found = None
    for item in history if history is not None else store.read_history():
        if item.number == number:
            found = item  # History is oldest first; the last entry wins.
    return found


def collect(store, history: Iterable[Any] | None = None) -> Questions:
    """Every open question's recipient. One malformed question file, flag or run record is skipped alone."""
    from foragent.orch_inbox import scan

    rows = store.local_issues()
    found: dict[int, str] = {}
    unlinked = 0
    answers: dict[int, list[datetime]] = {}
    for row in scan(store, "questions")[0]:
        linked = row.get("issues")
        numbers = [number for number in linked if type(number) is int] if isinstance(linked, list) else []
        if row["status"] == "open":
            found.update((number, PERSON) for number in numbers)
            unlinked += not numbers
        elif (at := _time(row.get("answered_at"))) is not None:
            for number in numbers:
                answers.setdefault(number, []).append(at)
    history = list(history) if history is not None else None
    for row in rows:
        number = row["number"]
        if row.get("state") != "open" or number in found:
            continue
        if NEEDS_HUMAN in (row.get("labels") or ()):
            routed = _routed(row)
            answered = bool(answers.get(number)) and (routed is None or max(answers[number]) >= routed)
            found[number] = ANSWERED if answered else PERSON
            continue
        try:
            flag = store.load_flag(number)
            run = _latest_run(store, number, history) if flag is not None and flag.reason == SPEC else None
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if run is None or run.outcome != SPEC:  # A running or later run has moved on.
            continue
        flagged = _flagged(store, number, flag.flagged_at)
        answered = flagged is not None and any(at >= flagged for at in answers.get(number, ()))
        found[number] = ANSWERED if answered else ORCHESTRATOR
    return Questions(found, unlinked)


def recipients(store, history: Iterable[Any] | None = None) -> dict[int, str]:
    """Issue number -> PERSON, ORCHESTRATOR or ANSWERED for every issue with a question."""
    return collect(store, history).issues


def _routed(row: dict[str, Any]) -> datetime | None:
    """When the issue was last routed to the person: its newest needs-human marker comment, if recorded."""
    times = []
    for comment in row.get("comments") or ():
        body = comment.get("body") if isinstance(comment, dict) else None
        if isinstance(body, str) and body and ROUTED.fullmatch(body.splitlines()[0].strip()):
            if (at := _time(comment.get("created_at"))) is not None:
                times.append(at)
    return max(times, default=None)


def _flagged(store, number: int, flagged_at: str) -> datetime | None:
    """When the flag was saved: the earlier of its recorded time and its file's write time."""
    times = [moment for moment in (_time(flagged_at),) if moment is not None]
    try:
        written = (store.root / "flags" / f"{number}.json").stat().st_mtime
        times.append(datetime.fromtimestamp(written, UTC))
    except OSError:
        pass
    return min(times, default=None)


def waiting(found: dict[int, str], unlinked: int = 0) -> dict[str, int]:
    """How many questions each recipient has; answered ones count for nobody, unlinked ones for the person."""
    counts = {who: sum(value == who for value in found.values()) for who in (PERSON, ORCHESTRATOR)}
    counts[PERSON] += unlinked
    return counts
