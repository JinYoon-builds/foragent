"""Work in the four-part approval-to-merge split: value work and wasted work.

Each merged issue's time from its latest approval to its merge is cut at every recorded
boundary; every slice belongs to exactly one part, so the four parts add up to the total.
This module classifies work (and owner merging, which is necessary waiting). Every slice
no work or merging span covers is wasted waiting here; telling necessary waits apart is
the waits part's job.

Sources, all kept by the gate already:

* phase events (orch-events ``phase``) and the open phase of a running run;
* ``activity`` flow records: the runner's and the merge owner's timed work and, for a base
  refresh, whether the move was ``neutral``. CI is read only from appended ``activity: ci``
  run records: one per run, with the ``head`` it tested, its ``result`` and the issues it
  covers (the runner's own issue, or ``issues`` for an owner or train run);
* review verdict events, ``ci-failure`` flow records and failed CI run records;
* ``merge-batch`` flow records: whether a train tip covering the issue was used or retired.

A round is one piece of work, not one phase span. A wait (any phase outside ``WORK``)
that interrupts a phase does not end it: the resumed span of the same phase continues
the round and keeps its kind. A round ends at a different work span, a review FAIL, a CI
failure or a new execution of the issue; a recorded work identity (``event_id``) decides
by itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any, NamedTuple

from foragent import orch_events
from foragent.orch_facts_resources import _jsonl, _time
from foragent.orch_scorecard_records import Interval, approval_before
from foragent.state import StateStore

DAY = timedelta(days=1)
PARTS = ("value_work", "wasted_work", "necessary_waiting", "wasted_waiting")
# Work phases and work activities. Any other phase or activity is a wait.
WORK = frozenset({"implementing", "reviewing", "checking", "ci", "resolving-conflict", "base-refresh", "merging"})
# Activities that name their own kind of work, so they win over the phase around them.
OWN_KIND = frozenset({"base-refresh", "merging"})
RERUN_CAUSES = frozenset({"base-changed", "group-failed"})
UNUSED = frozenset({"superseded", "void"})  # CI results nobody could use.
WAIT_RANK = 3  # A worker's wait phase is waiting, even inside its own activity block that spans it.
OWNER_RANK = 4  # The merge owner works in its own process: a worker's wait never hides that work.


class Span(NamedTuple):
    name: str
    start: datetime
    end: datetime
    rank: int  # Which span explains a moment covered twice: higher wins, then the later start.
    identity: str | None = None  # The round's recorded identity: a phase event_id or its activity record.
    data: dict | None = None  # The activity record, when the span is one.


def _phases(store: StateStore, now: datetime) -> dict[int, list[tuple[str, Interval, str | None]]]:
    """Each issue's phase spans with their work identity; a re-journaled close counts once."""
    unique: dict[tuple, tuple[int, str, Interval, str | None]] = {}
    for event in _jsonl(store.root / "orch-events.jsonl"):
        opened, closed = _time(event.get("phase_started_at")), _time(event.get("at"))
        if (event.get("kind") == "phase" and isinstance(event.get("phase"), str)
                and isinstance(event.get("issue"), int) and opened and closed):
            # The phase is in the key: an instant phase shares its start with the next one.
            unique[(event["issue"], event["phase"], event["phase_started_at"])] = (
                event["issue"], event["phase"], (opened, closed), event.get("event_id"))
    for run in store.active_runs():
        opened = _time(run.phase_started_at)
        if run.status == "running" and run.phase and opened:
            unique.setdefault((run.number, run.phase, run.phase_started_at),
                              (run.number, run.phase, (opened, now), None))
    out: dict[int, list] = {}
    for issue, name, span, identity in unique.values():
        out.setdefault(issue, []).append((name, span, identity))
    return out


def _activity_key(data: dict) -> str:
    return f"activity:{data.get('activity')}:{data.get('started_at')}"


def _activities(records: list[dict[str, Any]], number: int) -> list[Span]:
    spans = []
    for row in records:
        data = row["data"]
        name = data.get("activity")
        start, end = _time(data.get("started_at")), _time(data.get("finished_at"))
        if row["kind"] == "activity" and name in WORK and start is not None and end is not None and start < end:
            # Work another process or another issue's run did for this issue (the merge owner, a
            # train, a group leader's joint CI) is never hidden by this issue's own wait phases.
            other = data.get("source") == "merge-owner" or row["issue"] != number
            rank = OWNER_RANK if other else 2 if name in OWN_KIND else 0
            spans.append(Span(name, start, end, rank, _activity_key(data), data))
    return spans


def issue_records(records: list[dict[str, Any]], number: int) -> list[dict[str, Any]]:
    """The issue's own flow records plus the run records that name it among the issues they cover
    (an owner or train CI run, a train's outcome)."""
    def covers(data):
        # Run records name issues as {"n": N} rows; merge-batch records list bare issue numbers.
        return any((item.get("n") if isinstance(item, dict) else item) == number
                   for item in [*(data.get("issues") or []), *(data.get("members") or [])])

    return [row for row in records if row["issue"] == number or covers(row["data"])]


def _train(records: list[dict[str, Any]], head: str | None, number: int) -> str | None:
    """``used`` or ``retired`` when ``head`` is a train tip that covered the issue, else None."""
    for row in records:
        data = row["data"]
        if row["kind"] != "merge-batch" or not head or data.get("candidate") != head:
            continue
        for index, member in enumerate(data.get("members") or []):
            if member.get("n") == number:
                used = data.get("result") == "used" and index < (data.get("used_members") or 0)
                return "used" if used else "retired"
    return None


def _round_breaks(records, reviews, starts) -> list[datetime]:
    """Moments that end every round in progress: review FAILs, CI failures, new executions."""
    breaks = [at for event in reviews if event.get("verdict") == "FAIL" and (at := _time(event.get("at")))]
    breaks += [at for row in records if row["kind"] == "ci-failure" and (at := _time(row["at"]))]
    breaks += [end for row in records if row["kind"] == "activity" and row["data"].get("activity") == "ci"
               and row["data"].get("result") == "fail" and (end := _time(row["data"].get("finished_at")))]
    return [*breaks, *starts[1:]]


def classify(approved: datetime, merged: datetime, phases, records: list[dict[str, Any]],
             reviews: list[dict[str, Any]], starts: list[datetime], number: int) -> list[tuple[Span, str]]:
    """Every work and wait span inside the interval with its part, in time order."""
    activities = _activities(records, number)
    # CI time comes only from appended CI run records (runner, owner and train alike), never
    # from a ci phase or from fields a later write replaces.
    spans = [Span(name, a, b, 1, identity or _phase_activity(name, a, b, activities))
             for name, (a, b), identity in phases if name in WORK and name != "ci"]
    # Another activity whose phase is recorded is the phase's identity, not its own span: the
    # phase fragments say when that work ran, and the waits between them stay waits.
    spans += [span for span in activities if span.rank > 0 or span.name == "ci" or not any(
        name == span.name and a < span.end and span.start < b for name, (a, b), _ in phases)]
    waits = [Span(name, a, b, WAIT_RANK) for name, (a, b), _ in phases if name not in WORK and a < b]
    breaks = _round_breaks(records, reviews, sorted(starts))
    verdicts = sorted(at for event in reviews if (at := _time(event.get("at"))) is not None)
    out: list[tuple[Span, str]] = []
    previous: dict[str, tuple[Span, str]] = {}
    heads: dict[str, str] = {}
    built_since_ci = False
    for span in sorted(spans, key=lambda item: (item.start, item.rank)):
        prior = previous.get(span.name)
        continues = prior is not None and _continues(prior[0], span, spans, breaks)
        part = _part(span, prior, continues, records, heads, verdicts, built_since_ci, out, number)
        if span.name in {"implementing", "resolving-conflict"}:
            built_since_ci = True
        elif span.name == "ci":
            built_since_ci = False
        previous[span.name] = (span, part)
        out.append((span, part))
    out += [(span, "wasted_waiting") for span in waits]
    return [(span, part) for span, part in out
            if max(span.start, approved) < min(span.end, merged)]


def _phase_activity(name: str, start: datetime, end: datetime, activities: list[Span]) -> str | None:
    """The same-name activity record this phase fragment belongs to (most overlap), if any."""
    overlap = [(min(end, span.end) - max(start, span.start), span.identity) for span in activities
               if span.name == name and span.start <= end and start <= span.end]
    overlap = [item for item in overlap if item[0] > timedelta(0) or start == end]
    return max(overlap, key=lambda item: item[0])[1] if overlap else None


def _continues(prior: Span, span: Span, spans: list[Span], breaks: list[datetime]) -> bool:
    if prior.identity or span.identity:
        # A recorded identity on either side decides: one record is one round, however split.
        return prior.identity == span.identity
    if any(prior.end <= at <= span.start for at in breaks):
        return False
    # Instant work spans count too: they still separate two rounds.
    return not any(other.name != span.name and prior.end <= other.start and other.end <= span.start
                   and (other.start, other.end) != (prior.start, prior.end) for other in spans)


def _ci_heads(span: Span, records: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    data = [span.data] if span.data is not None else [
        row["data"] for row in records if row["kind"] == "activity" and row["data"].get("activity") == "ci"
        and _activity_key(row["data"]) == span.identity]
    heads = {item["head"] for item in data if item.get("head")}
    return heads, {item.get("cause") for item in data}


def _part(span, prior, continues, records, heads, verdicts, built_since_ci, earlier, number) -> str:
    name = span.name
    if name == "merging":
        return "necessary_waiting"
    if continues:
        # The same round resumed after a wait keeps its kind, whatever caused the round.
        return prior[1]
    if name == "ci":
        return _ci_part(span, prior, records, heads, built_since_ci, number)
    if name == "base-refresh":
        # A substantive refresh integrates main's change; a neutral one re-checks unchanged code.
        return "wasted_work" if (span.data or {}).get("neutral") else "value_work"
    if name == "resolving-conflict":
        return "wasted_work"
    if name in {"implementing", "reviewing"}:
        return "value_work" if prior is None else "wasted_work"
    if name == "checking":
        reviewed = any(at <= span.start for at in verdicts)
        tested = any(part_span.name == "ci" and part_span.start <= span.start for part_span, _ in earlier)
        return "value_work" if prior is None and not reviewed and not tested else "wasted_work"
    raise AssertionError(name)


def _ci_part(span, prior, records, heads, built_since_ci, number) -> str:
    """A new CI run: a new head is value, a rerun on a known head after a base move is wasted."""
    data = span.data or {}
    if data.get("result") in UNUSED or _train(records, data.get("head"), number) == "retired":
        return "wasted_work"  # Its result was never used.
    if data.get("stage") == "comparison" and ("latest", data.get("head")) in heads:
        # Waiting for the acceptance reports belongs to the head's latest CI run.
        return heads["latest", data["head"]]
    known, causes = _ci_heads(span, records)
    if known:
        part = ("value_work" if known - {key for key in heads if isinstance(key, str)}
                else "wasted_work" if causes & RERUN_CAUSES
                else heads[next(iter(known))])  # A resumed poll of the same head keeps its kind.
        for head in known:
            heads.setdefault(head, part)  # The head's first kind: what a resumed poll keeps.
            heads["latest", head] = part
        return part
    # An older record without a head: judge by its cause and what happened since the last CI.
    if causes & RERUN_CAUSES and prior is not None:
        return "wasted_work"
    return "value_work" if prior is None or built_since_ci else "wasted_work"


def parts(approved: datetime, merged: datetime, phases, records: list[dict[str, Any]],
          reviews: list[dict[str, Any]], starts: list[datetime], number: int) -> dict[str, float]:
    """Minutes of each part between approval and merge; they add up to the whole interval."""
    spans = classify(approved, merged, phases, records, reviews, starts, number)
    points = {approved, merged, *(at for span, _ in spans for at in (span.start, span.end))}
    points = sorted(at for at in points if approved <= at <= merged)
    totals = dict.fromkeys(PARTS, 0.0)
    for low, high in zip(points, points[1:]):
        covering = [(span, part) for span, part in spans if span.start <= low and high <= span.end]
        part = max(covering, key=lambda item: (item[0].rank, item[0].start))[1] if covering else "wasted_waiting"
        totals[part] += (high - low).total_seconds() / 60
    return totals


def _median(values):
    known = [value for value in values if value is not None]
    return median(known) if known else None


def windows(store: StateStore, now: datetime, days: int, issues, executions) -> list[dict[str, Any]]:
    """Per 24-hour bucket (today first): each merged issue's four parts and the median efficiency."""
    records, reviews, phases = store.flow_records(), orch_events.events(store, "review"), _phases(store, now)
    out = [{"median": None, "issues": []} for _ in range(days)]
    merges = {(row["number"], row["merged_at"]) for row in executions
              if row["merged"] and row["merged_at"] is not None and row["merged_at"] <= now}
    for number, merged in sorted(merges, key=lambda key: (key[1], key[0])):
        day = int((now - merged) // DAY)
        if day >= days or (approved := approval_before(store, issues, number, merged)) is None:
            continue
        starts = [row["started"] for row in executions if row["number"] == number and approved <= row["started"] <= merged]
        value = parts(approved, merged, phases.get(number, []), issue_records(records, number),
                      [event for event in reviews if event.get("issue") == number], starts, number)
        denominator = value["value_work"] + value["wasted_work"] + value["wasted_waiting"]
        out[day]["issues"].append({"issue": number, **value,
                                   "efficiency": value["value_work"] / denominator if denominator else None})
    for day in out:
        day["median"] = _median(entry["efficiency"] for entry in day["issues"])
    return out


def previous(days: list[dict[str, Any]]) -> dict[str, Any]:
    """The previous window: every issue of its days and the median of the daily medians."""
    return {"median": _median(day["median"] for day in days),
            "issues": [entry for day in days for entry in day["issues"]]}
