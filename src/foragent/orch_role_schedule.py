"""Roles that wake when an order finishes (D99): the shared due, hold and retry rules.

An order is finished when every issue its plan marker names is merged or closed. The
gate tick is the only writer of ``order-finished`` events: it appends one per order, the
first time it sees that order finished, with the time the last child closed. Later edits
to a closed child therefore do not make an old order look newly finished. Journal lines
live 30 days, so an order older than that is not recorded again and never wakes anything.

Each role compares those events with its own completion and stores nothing else:

- the architect and the docs role are due for an order that finished after their last
  successful session's input cutoff (its start, so records arriving during a session stay
  due). The session itself writes that ``<role>-done`` event; the architect also needs a new
  merge or run record after the cutoff. Each takes a machine slot the ready issues left free.
- the retrospective is one inbox request for the orchestrator, with no machine slot. Only a
  marked note (``retrospective`` on a ``note`` event) completes it: a request or its closing
  does not. A note answers the requests posted since the note before it and covers only the
  orders they name, so an order that finished meanwhile still gets its own request. A note
  with no such request covers every order finished by then.

Lane dreams use ``orders_finished_at`` too but keep their own start path in ``orch_dream``.

A role wakes only when ``.foragent.yml`` lists it: a config from before roles derives the default
entries for commands a caller runs by hand, and shows here as unavailable.

``scheduled_roles``, any live plan pause, quota, an executor pause and run hours hold these
wakes; ``--no-start`` ticks never reach ``dispatch``. A failed start or session waits one UTC
hour, or until a later executor reset. A capacity deferral is not a failure.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
import json
import logging
import re
from typing import Any

from foragent import orch_architect, orch_inbox, orch_plan
from foragent.board_parents import plan_references
from foragent.config import Config, in_run_hours
from foragent.i18n import text
from foragent.labels import APPROVED
from foragent.orch_events import append_event
from foragent.orch_role_prompt import ARCHITECT, _entry
from foragent.state import StateStore
from foragent.state_observations import stamp
from foragent.tracker import TrackerError, local_issue_models

log = logging.getLogger(__name__)

ORDER_FINISHED = "order-finished"
FAILED = "role-schedule-failed"
DOCS = "docs"
ORCHESTRATOR = "orchestrator"
RETROSPECTIVE = "retrospective"
SESSION_ROLES = (ARCHITECT, DOCS)  # The order in which an order's finished sessions start.
RETRY_AFTER = timedelta(hours=1)
KEEP = timedelta(days=30)  # How long the journal keeps an order-finished event.
_ORDER_ID = re.compile(r"<!-- foragent:plan id=(\S+) ")
_CHILD_ORDER_ID = re.compile(r"<!-- foragent:plan-child id=(\S+) ")


def finished_orders(issues: Iterable[Any]) -> dict[int, set[int]]:
    """Each published order whose children are all closed, with those children.

    An approved or lane-labelled issue with a plan marker is ordinary work. A child that is
    not in ``issues`` is unknown, so its order is not finished yet.
    """
    issues = [issue for issue in issues if not getattr(issue, "is_pull_request", False)]
    states = {issue.number: issue.state for issue in issues}
    orders = {}
    for issue in issues:
        if APPROVED in issue.labels or any(label.startswith("lane:") for label in issue.labels):
            continue
        children = (plan_references(issue.body) or set()) - {issue.number}
        if children and all(states.get(child) == "closed" for child in children):
            orders[issue.number] = children
    return orders


def _configured(config: Config, role: str) -> bool:
    return role in config.roles and _entry(config, role) is not None


def _events(store: StateStore) -> list[dict]:
    try:
        lines = (store.root / "orch-events.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and stamp(event.get("at")) is not None:
            events.append(event)
    return events


def _orders(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("kind") == ORDER_FINISHED
            and stamp(event.get("finished_at")) is not None]


def orders_finished_at(store: StateStore, issues: Iterable[Any]) -> list[tuple[set[int], datetime]]:
    """Each finished order's children and when it finished: its recorded time, else its children's latest
    listed change. An order with neither is past the journal's lifetime, so it wakes nothing."""
    issues = list(issues)
    changed = {issue.number: stamp(getattr(issue, "updated_at", None)) for issue in issues}
    recorded = {event["order"]: stamp(event["finished_at"]) for event in _orders(_events(store))}
    found = []
    for number, children in finished_orders(issues).items():
        at = recorded.get(number) or max((changed[child] for child in children if changed[child]), default=None)
        if at is not None:
            found.append((children, at))
    return found


def _closed_at(row: dict, now: datetime) -> datetime:
    """When a closed child closed: the listing's time when a listing saw it closed, else when first seen closed."""
    observed = row.get("observed") or {}
    listed = stamp(observed.get("updated_at")) if observed.get("state") == "closed" else None
    return listed or stamp(row.get("updated_at")) or now


def _read_unlisted_orders(ctx) -> None:
    """Read the order of a child closed since the last listing when no listed issue is that order.

    A full listing shows every order, so only a child closed after it can name an unknown one;
    the next listing ends these reads whatever they found.
    """
    if getattr(ctx, "_refresh_failed", False):
        return
    rows = ctx.store.local_issues()
    known = {match[1] for row in rows if (match := _ORDER_ID.search(row.get("body") or ""))}
    listed = stamp(ctx.store.issues_observation()["as_of"])
    for row in rows:
        match = _CHILD_ORDER_ID.search(row.get("body") or "")
        if (match is None or match[1] in known or row.get("state") != "closed"
                or (listed is not None and (stamp(row.get("updated_at")) or listed) <= listed)):
            continue
        known.add(match[1])
        try:
            ctx.tracker.parent_issue(row["number"])  # The tracker records what the read shows.
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise
            log.warning("Could not read the order of #%s: %s", row["number"], exc)


def observe(ctx) -> None:
    """Append one ``order-finished`` event for each order first seen finished; nothing starts here."""
    _read_unlisted_orders(ctx)
    now = ctx.now()
    recorded = {event.get("order") for event in _orders(_events(ctx.store))}
    rows = {row["number"]: row for row in ctx.store.local_issues()}
    for number, children in sorted(finished_orders(local_issue_models(ctx.store)).items()):
        if number in recorded:
            continue
        finished_at = max(_closed_at(rows[child], now) for child in children)
        if now - finished_at >= KEEP:
            continue
        append_event(ctx.store, now, number, ORDER_FINISHED, f"Order #{number} finished",
                     order=number, children=sorted(children),
                     finished_at=finished_at.astimezone(UTC).isoformat())


def _last_success(events: list[dict], role: str) -> datetime | None:
    """The input cutoff of the role's latest successful session: when that session started."""
    cutoffs = [stamp(event.get("covered_until") or event.get("started_at"))
               for event in events if event.get("kind") == f"{role}-done" and event.get("exit_code") == 0]
    return max((cutoff for cutoff in cutoffs if cutoff is not None), default=None)


def _attempts(events: list[dict], role: str) -> tuple[datetime | None, datetime | None, str | None]:
    """The last attempt, and the failure and error that hold the role's next one, if any.

    A call no session answered counts as failed at the time of the call: its session died
    before it could write its ``<role>-done`` event.
    """
    attempt, failure, error = None, None, None
    for event in events:
        kind, at = event.get("kind"), stamp(event.get("at"))
        if kind == f"{role}-call":
            attempt, failure, error = at, at, None
        elif kind == FAILED and event.get("role") == role:
            attempt, failure, error = at, at, event.get("error") or event.get("summary")
        elif kind == f"{role}-done":
            success = event.get("exit_code") == 0
            failure, error = (None, None) if success else (at, event.get("summary"))
    return attempt, failure, error


def _next_retry(store: StateStore, config: Config, failure: datetime | None, now: datetime) -> datetime | None:
    """One UTC hour after a failure, or a later executor reset; None when nothing holds a retry."""
    if failure is None:
        return None
    floor = failure + RETRY_AFTER
    reset = store.executor_pause_until(config.executor, now)
    return max(floor, reset) if reset is not None else floor


def _has_input(store: StateStore, since: datetime | None, order: dict) -> bool:
    """A run record for this finished order: new since the cutoff and no later than the order was seen
    finished, or for one of its children. A later run of other work waits for its own order to finish."""
    times = store.finish_times()
    seen, children = stamp(order["at"]), set(order.get("children") or [])
    for run in store.read_history():
        if run.kind != "issue":
            continue
        finished = stamp(times.get((run.number, run.started_at)))
        if (finished is not None and (since is None or finished > since)
                and (finished <= seen or run.number in children)):
            return True
    return False


def suppression(store: StateStore, config: Config, now: datetime) -> str | None:
    """Why this project's scheduled roles are held now, or None."""
    from foragent.orch_dream import _quota_exhausted

    if not config.scheduled_roles:
        return "switch"
    if orch_plan.load(store, now)["paused"]:
        return "plan_pause"
    if not in_run_hours(now.astimezone(), config.run_hours):
        return "run_hours"
    if _quota_exhausted(store, now):
        return "quota"
    if store.executor_paused(config.executor, now):
        return "executor_pause"
    return None


def _session_facts(store: StateStore, config: Config, role: str, now: datetime, events: list[dict]) -> dict:
    cutoff = _last_success(events, role)
    orders = [event for event in _orders(events)
              if cutoff is None or stamp(event["finished_at"]) > cutoff]
    attempt, failure, error = _attempts(events, role)
    retry = _next_retry(store, config, failure, now)
    facts = {"role": role, "available": _configured(config, role), "last_success": _iso(cutoff),
             "input_cutoff": _iso(cutoff), "last_attempt": _iso(attempt), "last_error": error,
             "pending_age_seconds": None}
    if role == ARCHITECT:
        reviewable = [event for event in orders if _has_input(store, cutoff, event)]
        reason = "order_finished" if reviewable else "no_new_input" if orders else "no_finished_order"
        orders = reviewable or orders
    else:
        reason = "order_finished" if orders else "no_finished_order"
    running = _session_is_live(store, role)
    if running:
        retry = None  # A live session has not failed; its call only holds a retry once it is gone.
    return {**facts, "orders": [event["order"] for event in orders], "next_retry": _iso(retry), "running": running,
            "due": reason == "order_finished", "reason": "running" if running else reason,
            "waiting_retry": retry is not None and now < retry}


def _retrospective_requests(store: StateStore) -> list[dict]:
    return [row for row in orch_inbox.requests(store) if row.get("kind") == RETROSPECTIVE]


def _request_time(row: dict) -> datetime | None:
    return stamp(row.get("created_at"))


def _retrospective_facts(store: StateStore, config: Config, now: datetime, events: list[dict]) -> dict:
    notes = sorted(stamp(event["at"]) for event in events
                   if event.get("kind") == "note" and event.get("retrospective") is True)
    last = notes[-1] if notes else None
    requests = _retrospective_requests(store)
    covered, covered_until, previous = set(), None, None
    for note in notes:
        answered = [row for row in requests if (at := _request_time(row)) is not None and at <= note
                    and (previous is None or at > previous)]
        if answered:
            covered.update(order for row in answered for order in row.get("orders") or [])
        else:
            covered_until = note
        previous = note
    orders = [event["order"] for event in _orders(events) if event["order"] not in covered
              and (covered_until is None or stamp(event["finished_at"]) > covered_until)]
    # A marked note answered every request older than it, even one still open; it holds nothing back.
    pending = next((row for row in requests if row["status"] != "done"
                    and (last is None or (_request_time(row) or now) > last)), None)
    # A request closed with no marked note since it was posted was not a retrospective; ask again after the floor.
    unanswered = [_closed_time(row) for row in requests if row["status"] == "done"
                  and (last is None or (_request_time(row) or now) > last)]
    attempt, failure, error = _attempts(events, RETROSPECTIVE)
    posted = max((time for time in map(_request_time, requests) if time is not None), default=None)
    if failure is not None and posted is not None and posted >= failure:
        failure, error = None, None  # A request posted since then answers that failure.
    failure = max((time for time in (failure, *unanswered) if time is not None), default=None)
    retry = _next_retry(store, config, failure, now)
    pending_at = _request_time(pending) if pending is not None else None
    reason = "pending_request" if pending is not None else "order_finished" if orders else "no_finished_order"
    return {"role": RETROSPECTIVE, "available": _configured(config, ORCHESTRATOR),
            "last_success": _iso(last), "input_cutoff": _iso(last), "orders": orders, "due": bool(orders),
            "reason": reason, "last_attempt": _iso(attempt or pending_at), "last_error": error,
            "next_retry": _iso(retry), "waiting_retry": retry is not None and now < retry,
            "pending": pending["id"] if pending is not None else None,
            "pending_age_seconds": (now - pending_at).total_seconds() if pending_at is not None else None}


def _closed_time(row: dict) -> datetime | None:
    notes = [stamp(note.get("at")) for note in row.get("notes") or []
             if isinstance(note, dict) and note.get("status") == "done"]
    return max((time for time in notes if time is not None), default=None)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def facts(store: StateStore, config: Config, now: datetime) -> dict:
    """What `fa orch facts` shows: each scheduled role's due, suppression and retry facts."""
    events = _events(store)
    held = suppression(store, config, now)
    roles = [_session_facts(store, config, role, now, events) for role in SESSION_ROLES]
    roles.append(_retrospective_facts(store, config, now, events))
    return {"enabled": config.scheduled_roles, "suppressed": held, "roles": roles}


def human_lines(section: dict, language: str) -> list[str]:
    """One localized line per configured role."""
    unknown = text("facts_schedule_unknown", language)

    def state(item: dict) -> str:
        if not item["available"]:
            return text("facts_schedule_unavailable", language)
        if section["suppressed"] and item["due"]:
            return text("facts_schedule_held", language, reason=section["suppressed"])
        return text(f"facts_schedule_{item['reason']}", language)

    return [text("facts_schedule_role", language, role=item["role"], state=state(item),
                 orders=", ".join(f"#{order}" for order in item["orders"]) or "-",
                 cutoff=item["input_cutoff"] or unknown, attempt=item["last_attempt"] or unknown,
                 retry=item["next_retry"] or "-", error=(item["last_error"] or "-")[:120],
                 age=unknown if item["pending_age_seconds"] is None else f"{item['pending_age_seconds']:.0f} s")
            for item in section["roles"]]


def _session_is_live(store: StateStore, role: str) -> bool:
    return orch_architect.session_alive(orch_architect.read_record(store, role))


def startable(ctx, sample) -> list[str]:
    """The session roles that may start now; at most one starts per tick, so they ask for one worker."""
    now = ctx.now()
    if sample[0] == "critical" or suppression(ctx.store, ctx.config, now) is not None:
        return []
    events = _events(ctx.store)
    roles = []
    for role in SESSION_ROLES:
        item = _session_facts(ctx.store, ctx.config, role, now, events)
        if item["available"] and item["due"] and not item["waiting_retry"] and not item["running"]:
            roles.append(role)
    ctx._dream_demand = getattr(ctx, "_dream_demand", 0) + min(1, len(roles))
    return roles


def _why(ctx, role: str, now: datetime) -> str:
    events = _events(ctx.store)
    item = _session_facts(ctx.store, ctx.config, role, now, events)
    orders = ", ".join(f"#{order}" for order in item["orders"])
    key = "schedule_why_architect" if role == ARCHITECT else "schedule_why_role"
    return text(key, ctx.config.language, role=role, orders=orders, since=item["input_cutoff"] or "-")


def _start_session(ctx, roles: list[str], budget: int, report) -> None:
    """Start the first due session in a slot left free; a failed start stays due after the retry floor."""
    from foragent import scheduler

    for role in roles:
        if not scheduler._has_slot(ctx, budget):
            return
        now = ctx.now()
        try:
            report.actions.append(orch_architect.call(ctx.repo_root, ctx.store, ctx.config,
                                                      _why(ctx, role, now), now, role))
        except orch_architect.ArchitectRefused as exc:  # No free slot after all: it stays due, with no penalty.
            report.actions.append(f"{role} deferred: {exc}")
            return
        except Exception as exc:  # A failed start stays due after the floor; it must not end the tick.
            error = " ".join(str(exc).split())[:200]
            append_event(ctx.store, now, None, FAILED, f"{role} did not start: {error}", role=role, error=error)
            report.actions.append(f"{role} did not start: {error}")
            continue
        return


def _post_retrospective(ctx, report) -> None:
    """Keep one pending retrospective request naming every finished order not yet reviewed."""
    now = ctx.now()
    if not _configured(ctx.config, ORCHESTRATOR) or suppression(ctx.store, ctx.config, now) is not None:
        return
    item = _retrospective_facts(ctx.store, ctx.config, now, _events(ctx.store))
    if not item["due"] or item["pending"] is not None or item["waiting_retry"]:
        return
    orders = ", ".join(f"#{order}" for order in item["orders"])
    try:
        request = orch_inbox.add_request(
            ctx.store, "gate", text("schedule_retrospective", ctx.config.language, orders=orders),
            {"kind": RETROSPECTIVE, "orders": item["orders"]})
    except (OSError, orch_inbox.InboxError) as exc:
        error = " ".join(str(exc).split())[:200]
        append_event(ctx.store, now, None, FAILED, f"retrospective not posted: {error}",
                     role=RETROSPECTIVE, error=error)
        report.actions.append(f"retrospective not posted: {error}")
        return
    report.actions.append(f"Retrospective due for {orders}; inbox request {request['id']}")


def dispatch(ctx, roles: list[str], budget: int, report) -> None:
    """After this tick's issues and dreams: start one due session, then post the retrospective."""
    _start_session(ctx, roles, budget, report)
    _post_retrospective(ctx, report)
