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
  to restart. The needs-human label may stay until someone acts on the answer.

An answer ends only a question raised before it: an answer older than the issue's
current question (its routing marker comment, its current spec flag or the start of
its latest run, whichever is newest) belongs to an earlier question and is ignored.
An answer also ends only a question of its own kind (``orch_inbox.QUESTION_KINDS``):
a merge or order decision about the issue never ends its plain question, and a plain
answer never ends a pending merge or order decision. A spec question is plain. A
needs-human question takes its kind from the reason recorded in its newest routing
marker (``reasons.GROUP``: a review is a merge decision, a re-approval an order
decision, anything else plain); without a marker it is plain.

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
# The inbox question kind of a plain question (a spec question, an escalation).
PLAIN = "question"
# The machine marker on the first line of a comment that routes an issue to the person.
ROUTED = re.compile(r"<!-- foragent:needs-human reason=([a-z-]+) -->")
# A routed question's inbox kind by its recorded reason group; every other group is plain.
KIND_BY_GROUP = {"review": "merge", "reapprove": "order"}


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
    answers: dict[int, list[tuple[datetime, str]]] = {}  # Issue -> (answered_at, question kind).
    for row in scan(store, "questions")[0]:
        linked = row.get("issues")
        numbers = [number for number in linked if type(number) is int] if isinstance(linked, list) else []
        if row["status"] == "open":
            found.update((number, PERSON) for number in numbers)
            unlinked += not numbers
        elif (at := _time(row.get("answered_at"))) is not None:
            for number in numbers:
                answers.setdefault(number, []).append((at, row.get("kind") or PLAIN))
    history = list(history) if history is not None else None
    for row in rows:
        number = row["number"]
        if row.get("state") != "open" or number in found:
            continue
        try:
            flag = store.load_flag(number)
            run = _latest_run(store, number, history)
        except (OSError, ValueError, TypeError, KeyError):
            flag = run = None
        # The current spec question: the flag says so and the latest run ended with it.
        spec = flag is not None and flag.reason == SPEC and run is not None and run.outcome == SPEC
        flagged = _flagged(store, number, flag.flagged_at) if spec else None
        if NEEDS_HUMAN in (row.get("labels") or ()):
            # Routed to the person: a decision of the question's own kind, made after it was raised.
            routed, kind = _routed(row)
            raised = [moment for moment in (routed, flagged, _time(run.started_at) if run else None) if moment]
            found[number] = ANSWERED if _answered(answers.get(number, ()), kind, max(raised, default=None)) else PERSON
        elif spec:
            # Not routed yet: a plain answer after the flag ends it. The flag is saved when the
            # run ends, after its start, so it is the question's newest fact.
            answered = flagged is not None and _answered(answers.get(number, ()), PLAIN, flagged)
            found[number] = ANSWERED if answered else ORCHESTRATOR
    return Questions(found, unlinked)


def _answered(answers: Iterable[tuple[datetime, str]], kind: str, raised: datetime | None) -> bool:
    """Whether a decision of ``kind`` came at or after the question was raised (unknown raise time: any)."""
    return any(given == kind and (raised is None or at >= raised) for at, given in answers)


def recipients(store, history: Iterable[Any] | None = None) -> dict[int, str]:
    """Issue number -> PERSON, ORCHESTRATOR or ANSWERED for every issue with a question."""
    return collect(store, history).issues


def _routed(row: dict[str, Any]) -> tuple[datetime | None, str]:
    """(when, inbox kind) of the issue's newest needs-human marker comment; (None, plain) without one."""
    from foragent.reasons import GROUP, Reason

    markers = []
    for comment in row.get("comments") or ():
        body = comment.get("body") if isinstance(comment, dict) else None
        match = ROUTED.fullmatch(body.splitlines()[0].strip()) if isinstance(body, str) and body else None
        if match and (at := _time(comment.get("created_at"))) is not None:
            group = GROUP.get(Reason(match[1])) if match[1] in Reason._value2member_map_ else None
            markers.append((at, KIND_BY_GROUP.get(group or "", PLAIN)))
    return max(markers, key=lambda marker: marker[0], default=(None, PLAIN))


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
