"""An order parent's board column follows its sub-issues.

A parent is an open, unapproved issue with sub-issues. Its own labels say
nothing about progress, so the gate derives its column from the children on
each tick that read the issue listing, and closes it once no child is open.
A child closed as not planned (for example replaced by a newer order) has
ended too; the one closing comment names completed and cancelled children in
separate groups. An approved issue with sub-issues stays ordinary work.

An order record (plan marker, no lane label) also owns every issue its plan
marker references: the map values and the issues in its acceptance (edit)
map. Edit-only records and records older than native sub-issue edges have no
sub-issues, so the close waits for those references as well (#837). A closed
reference or sub-issue counts as ended whatever its reason. While any child is
open, a cancelled child only flags the order for a decision. Until a record
closes, its card keeps today's handling.
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any

from foragent import board
from foragent.i18n import text
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, RUNNING
from foragent.orch_events import append_event
from foragent.tracker import Issue, TrackerError

log = logging.getLogger(__name__)

NOT_PLANNED_FLAG = "parent-child-not-planned"
_MARKER_START = "<!-- foragent:plan "
_PLAN_MARKER = re.compile(r"<!-- foragent:plan id=\S+ map=(\{[^\n]*?\})(?: acceptance=(\{[^\n]*\}))? -->")


def _completed(child: Issue) -> bool:
    # GitHub closes as completed by default; an unknown reason is that default.
    return child.state == "closed" and getattr(child, "state_reason", None) in {None, "completed"}


def parent_state(children: Iterable[Issue], open_prs: set[int] = frozenset()) -> str | None:
    """The gate state whose board column the parent shows, or None without children."""
    children = list(children)
    if not children:
        return None
    open_children = [child for child in children if child.state == "open"]
    completed = [child for child in children if _completed(child)]
    if any(NEEDS_HUMAN in child.labels for child in open_children):
        return "Needs human"
    if not open_children:
        return "Done"
    if completed or any(child.labels & {RUNNING, BLOCKED} or child.number in open_prs
                        for child in open_children):
        return "Running"
    if open_children and all(APPROVED in child.labels for child in open_children):
        return "Approved"
    return "unapproved"


def order_parents(rows: list[dict[str, Any]]) -> set[int]:
    """Open, unapproved issues that the last listing showed with sub-issues.

    The parent's own blocked or needs-human label does not take it out: its
    column, completion and cancellation flag still come from its children.
    """
    return {row["number"] for row in rows
            if row.get("state") == "open" and row.get("children")
            and APPROVED not in row.get("labels", ())}


def plan_references(body: str) -> set[int] | None:
    """Issues a plan marker names: its map values and its acceptance (edit) map keys.

    None when the body has a plan marker that does not parse; a marker with
    any invalid entry is unparseable as a whole, so no reference is dropped.
    """
    if _MARKER_START not in (body or ""):
        return set()
    marker = _PLAN_MARKER.search(body)
    if marker is None:
        return None
    try:
        mapping = json.loads(marker[1])
        acceptance = json.loads(marker[2]) if marker[2] else {}
    except ValueError:
        return None
    if (not isinstance(mapping, dict) or not isinstance(acceptance, dict)
            or any(type(n) is not int or n <= 0 for n in mapping.values())
            or any(not isinstance(key, str) or not key.isdecimal() or int(key) <= 0 for key in acceptance)):
        return None
    return set(mapping.values()) | {int(key) for key in acceptance}


def order_records(rows: list[dict[str, Any]]) -> tuple[dict[int, set[int]], set[int]]:
    """Open order records (plan marker, no lane label) with their references, and those whose marker does not parse.

    An approved issue is ordinary work even with a plan marker, as with sub-issues.
    A record may reference itself; it is open, so that keeps it open.
    """
    records, unparseable = {}, set()
    for row in rows:
        labels = row.get("labels", ())
        if (row.get("state") != "open" or APPROVED in labels
                or any(label.startswith("lane:") for label in labels)
                or _MARKER_START not in (body := row.get("body") or "")):
            continue
        references = plan_references(body)
        if references is None:
            unparseable.add(row["number"])
        else:
            records[row["number"]] = references
    return records, unparseable


def _closed_references(ctx, references: set[int], rows: dict[int, dict[str, Any]],
                       listed_prs: set[int]) -> dict[int, bool] | None:
    """Each referenced issue mapped to whether it was cancelled, if every one is closed now;
    None while any is open or unreadable.

    A closed reference counts as ended whatever its close reason; it is
    cancelled only when its reason is known to be not_planned. Listed issues
    answer from the listing, and only when all of them are closed is each
    unlisted reference read fresh, in this tick; nothing is kept between
    ticks. A number that reads as a pull request is ignored.
    """
    closed, unlisted = {}, []
    for number in sorted(references - listed_prs):
        row = rows.get(number)
        if row is None:
            unlisted.append(number)
        elif row.get("state") != "closed":
            return None  # Open work keeps the record open without spending reads.
        else:
            closed[number] = row.get("state_reason") == "not_planned"
    for number in unlisted:
        try:
            issue = ctx.tracker.get_issue(number)
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise
            log.warning("Could not read #%s, which an order record references: %s", number, exc)
            return None
        if issue.is_pull_request:
            continue
        if issue.state != "closed":
            return None
        closed[number] = getattr(issue, "state_reason", None) == "not_planned"
    return closed


def closing_comment(language: str, completed: list[int], cancelled: list[int]) -> str:
    """The one closing comment: the shared record line, then each non-empty group of children."""
    parts = [text("order_record_closed", language, count=len(completed) + len(cancelled))]
    for key, group in (("order_closed_completed", completed), ("order_closed_cancelled", cancelled)):
        if group:
            parts.append(text(key, language, children=", ".join(f"#{child}" for child in group)))
    return " ".join(parts)


def _summary(row: dict[str, Any]) -> dict[str, int]:
    return (row.get("observed") or {}).get("sub_issues") or {}


def _with_counted_open_work(row: dict[str, Any], children: list[Issue]) -> list[Issue]:
    """The children shown, plus the open work the native summary counts and they do not show.

    GitHub's completed count includes cancelled children, so completed < total
    means some child is open. The summary only ever adds open work; it never
    closes anything by itself.
    """
    summary = _summary(row)
    total = summary.get("total")
    if (total is None or summary.get("completed", total) >= total
            or any(child.state == "open" for child in children)):
        return children
    return [*children, Issue(0, "", "", frozenset(), "open")]  # Stands for the unseen open work.


def _listed_issue(row: dict[str, Any]) -> Issue:
    return Issue(row["number"], row.get("title") or "", row.get("body") or "", frozenset(row.get("labels", ())),
                 row.get("state", "open"), state_reason=row.get("state_reason"))


def _listed_children(row: dict[str, Any], rows: dict[int, dict[str, Any]]) -> list[Issue] | None:
    """A parent's children as this tick's listing shows them; None when only a read can tell.

    Child rows carry state, close reason and labels, so a child-only change is
    seen even if the parent's own row did not change.
    """
    numbers = row.get("children") or []
    known = [_listed_issue(rows[number]) for number in numbers if number in rows]
    total = _summary(row).get("total")
    if total is not None and total != len(numbers):
        return None
    if len(known) == len(numbers):
        return known
    counted = _with_counted_open_work(row, known)
    return counted if counted is not known else None


def _issue_facts(issue: Issue) -> dict[str, Any]:
    return {"number": issue.number, "state": issue.state, "state_reason": getattr(issue, "state_reason", None),
            "labels": sorted(issue.labels), "is_pull_request": issue.is_pull_request}


def _issue_from(facts: dict[str, Any]) -> Issue:
    return Issue(facts["number"], "", "", frozenset(facts["labels"]), facts["state"],
                 facts["is_pull_request"], state_reason=facts["state_reason"])


class _ActionCalls:
    """The tracker as one parent's action sees it: a completed read is not repeated.

    The one rule for every parent action, however many calls it needs: each
    completed read is remembered with the listing facts it depended on (the
    existing per-issue read memory), so a tick that runs out of budget continues
    after the last completed call. Three things end a memory, because each shows
    the facts behind it changed:

    - The listing shows different facts for the parent or a linked row.
    - GitHub rejects a board write, which the listing cannot show: the card or
      field that the board reads named is gone. Running out of time is no rejection.
    - The action ran to its end, for its issue reads: what it waits on outside
      the listing can change without the listing showing it (#837).

    Writes are never remembered. Each one is sent until it succeeds, and sending
    an accepted write again is safe.
    """

    def __init__(self, ctx, number: int, links: list[int], listed: Callable[[int], bool]):
        self._tracker, self._store, self._number = ctx.tracker, ctx.store, number
        self._links, self._listed = list(links), listed
        self._issue_reads: set[str] = set()
        self._board_reads: set[str] = set()
        self.interrupted = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tracker, name)

    def _read(self, name: str, arguments: Any, call, encode, decode, links=lambda result: ()) -> Any:
        key = f"parent-action:{name}:" + hashlib.sha256(
            json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest()
        {"get_issue": self._issue_reads, "graphql": self._board_reads}.get(name, set()).add(key)
        remembered = self._store.confirmed_read(self._number, key)
        if remembered is not None and remembered[0] is not None:
            result = decode(remembered[0])
            self._link(links(result))
            return result
        baseline = self._store.read_baseline()
        try:
            result = call()
        except TrackerError:
            self.interrupted = True
            raise
        self._link(links(result))
        self._store.record_read(self._number, key, encode(result), baseline=baseline, links=self._links)
        return result

    def _link(self, numbers: Iterable[int]) -> None:
        self._links.extend(n for n in numbers if n not in self._links and self._listed(n))

    def _forget(self, keys: Iterable[str]) -> None:
        baseline = self._store.read_baseline()
        for key in keys:
            self._store.record_read(self._number, key, None, baseline=baseline)

    def get_issue(self, number: int) -> Issue:
        return self._read("get_issue", number, lambda: self._tracker.get_issue(number), _issue_facts, _issue_from)

    def sub_issues(self, number: int) -> list[Issue]:
        # A child only this read names depends on its listed row too.
        return self._read("sub_issues", number, lambda: self._tracker.sub_issues(number),
                          lambda issues: [_issue_facts(issue) for issue in issues],
                          lambda facts: [_issue_from(item) for item in facts],
                          lambda issues: [issue.number for issue in issues if issue.number != number])

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if query.lstrip().startswith("query"):
            # A response that carries errors is no completed read.
            return self._read("graphql", [query, variables], lambda: self._tracker.graphql(query, variables),
                              lambda result: None if result.get("errors") else result, lambda result: result)
        try:
            result = self._tracker.graphql(query, variables)
        except TrackerError as exc:
            self.interrupted = True
            if not exc.is_rate_limit and (exc.returncode is not None or "GraphQL errors" in str(exc)):
                self._forget(self._board_reads)
            raise
        if result.get("errors"):
            self.interrupted = True
            self._forget(self._board_reads)
        return result

    def finish(self) -> None:
        if not self.interrupted and self._issue_reads:
            self._forget(self._issue_reads)


def follow_children(ctx, report) -> None:
    """Move each order parent's card to its children's column; close it when all its work is done.

    The listing is the fact source, so an unchanged parent spends none of the
    shared budget. Parents ready to close go first, lowest number first: a close
    happens once and leaves the list. The others follow in an order drawn from
    the tick's time, with nothing stored, so a parent that spends the budget on
    every tick cannot keep starving a later one. One parent's failure defers
    only that parent, and the step still reports it; a rate limit stops the step.
    """
    rows = ctx.store.local_issues()
    by_number = {row["number"]: row for row in rows}
    open_prs = {row["number"] for row in rows if row.get("pr_number") and not row.get("merged")}
    listed_prs = {row["pr_number"] for row in rows if row.get("pr_number")}
    parents = order_parents(rows)
    records, unparseable = order_records(rows)
    language = ctx.config.language

    def is_listed(number: int) -> bool:
        # Only a row the listing showed can confirm a read; a row from another read has no listed facts.
        return bool((by_number.get(number) or {}).get("observed"))

    def needs_read(number: int, children: list[Issue] | None) -> bool:
        if children is None:
            return True
        if number not in records or any(child.state != "closed" for child in children):
            return False
        references = records[number] - {child.number for child in children} - listed_prs
        return (all(by_number[n].get("state") == "closed" for n in references if n in by_number)
                and any(n not in by_number for n in references))

    def decide(view, number: int, children: list[Issue]) -> tuple[str | None, bool, dict[int, bool]]:
        # Until a record closes, only an order parent's sub-issues move its card (today's handling).
        state = parent_state(children, open_prs) if number in parents else None
        work = {child.number for child in children}
        ended = {child.number: not _completed(child) for child in children}  # number -> cancelled
        if number in records:
            done = None
            if all(child.state == "closed" for child in children):
                # Sub-issues already carry their state; read only the other references.
                done = _closed_references(view, records[number] - work, by_number, listed_prs)
            closes = done is not None and bool(work | done.keys())
            ended.update(done or {})
        elif number in unparseable:
            closes = False  # Its work is unknown: skip the close quietly; the card keeps today's column.
        else:
            closes = state == "Done"
        return state, closes, ended

    def act(number: int, calls: _ActionCalls) -> None:
        view = SimpleNamespace(tracker=calls, store=ctx.store, config=ctx.config, repo=getattr(ctx, "repo", None))
        children = listed[number]
        if children is None:
            # A row this listing showed is newer than any read of the child, remembered or not.
            children = _with_counted_open_work(by_number[number], [
                _listed_issue(by_number[child.number]) if is_listed(child.number) else child
                for child in calls.sub_issues(number)])
        moved = None
        if number in parents and needs_read(number, children):
            # The card's column depends only on the children, so it need not wait for reference reads.
            moved = parent_state(children, open_prs)
            if moved is not None:
                board.sync_status(view, number, moved, parent=True)
        state, closes, ended = decide(view, number, children)
        cancelled = [child.number for child in children
                     if child.state == "closed" and not _completed(child)]
        if cancelled and number in parents and not closes:  # A decision only while work remains.
            previous = ctx.store.load_local_flag(number)
            if previous is None or previous.reason != NOT_PLANNED_FLAG:
                at = ctx.now()
                ctx.store.save_flag(number, NOT_PLANNED_FLAG, flagged_at=at.isoformat(), local_only=True)
                summary = text("parent_child_not_planned", language, number=number,
                               children=", ".join(f"#{child}" for child in cancelled))
                append_event(ctx.store, at, number, NOT_PLANNED_FLAG, summary)
                report.actions.append(summary)
        if closes:
            completed = sorted(child for child, gone in ended.items() if not gone)
            cancelled = sorted(child for child, gone in ended.items() if gone)
            ctx.tracker.close_issue(number, closing_comment(language, completed, cancelled))
            report.actions.append(text("parent_closed_report", language, number=number,
                                       completed=len(completed), cancelled=len(cancelled)))
            state = "Done"
        if state is not None and state != moved:
            board.sync_status(view, number, state, parent=True)

    def visit(number: int) -> None:
        linked = [*(by_number[number].get("children") or []), *records.get(number, ())]
        calls = _ActionCalls(ctx, number, [n for n in dict.fromkeys(linked) if n != number and is_listed(n)], is_listed)
        try:
            act(number, calls)
        except TrackerError:
            calls.interrupted = True
            raise
        calls.finish()

    listed = {number: _listed_children(by_number[number], by_number) for number in parents | records.keys()}
    seed = ctx.now().isoformat()
    closing, others = [], []
    for number, children in listed.items():
        ready = not needs_read(number, children) and decide(ctx, number, children)[1]
        (closing if ready else others).append(number)

    def drawn(number: int) -> bytes:
        return hashlib.sha256(f"{seed}/{number}".encode()).digest()

    failure = None
    for number in [*sorted(closing), *sorted(others, key=drawn)]:
        try:
            visit(number)
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise
            failure = exc
    if failure is not None:
        raise failure  # The tick records the deferral; the next listing tick tries again.
