"""Acceptance tests for path-phase-records: every wait on the publish-to-merge path is recorded.

PM r984 (11-07): 63% of approval-to-merge issue-minutes were 'unknown' on the scorecard,
mostly merge-owner queue waits (including batch CI) and early flow records lost to the
two-generation flow file. After this issue a wait in the merge-owner queue, in pending batch
CI, in test preparation, as a pending publication, and between gate ticks has a phase record
that names the place, and the issue's flow records are kept from its approval to its merge.

Contract chosen by the lane (DECIDE; see the notes of this preparation):

* A phase record is a ``kind == "phase"`` journal event in the store's ``orch-events.jsonl``,
  the existing #224 schema the scorecard already reads: ``phase`` (the place), ``issue``,
  ``phase_started_at`` (start) and ``at`` (end), so it spans ``[phase_started_at, at]``.
  A record with the same (issue, phase, phase_started_at) is one interval journaled again.
* The place is named in ``phase``. The tests match words, not exact strings:
  merge-owner queue: contains "queue" (and is not a batch CI name);
  pending batch CI: contains "batch" or "train", and "ci";
  test preparation: contains "prepar"; pending publication: contains "publi";
  tick gap: contains "tick" and "gap".
* An order's phase record names the order: one of its other values is the order id
  (the plan id) or one of its publication request ids.
* Gate ticks run every 5 minutes (launchd StartInterval 300). A 5-minute step between ticks
  is not a gap; a 40-minute pause is.

Real gate tick, merge owner, batch train, runner, Git and state from the merge-owner
prototype world (only GitHub, models and the clock are fake); the real publication queue,
off-owner red check and owner pass from the returned-publication world; the scorecard
through `fa orch scorecard --json` and its collector. Expected minutes are hand-calculated.
"""

from datetime import datetime, timedelta

import pytest

from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from test_merge_owner_batch import batched, groups, queue_workers  # noqa: F401
from test_order_publications import case  # noqa: F401  (fixture)
from test_returned_publication_owner import (  # noqa: F401
    TITLE, owner, owner_passes, publication_ids, red, reprepare, returned, statuses, strings,
)
from test_scorecard_flow import NOW, flow, _approved, _read  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401

from foragent import orch_events


TICK = timedelta(minutes=5)
SLACK = 6  # minutes: one gate tick plus the fixture's one-second steps.


def _queue(name):
    return "queue" in name and not _batch_ci(name)


def _batch_ci(name):
    return ("batch" in name or "train" in name) and "ci" in name


def _preparation(name):
    return "prepar" in name


def _publication(name):
    return "publi" in name


def _tick_gap(name):
    return "tick" in name and "gap" in name


# ---------------------------------------------------------------- phase record helpers

def _time(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def phase_spans(store, places, *, issue=None, order=None):
    """Distinct [start, end] spans of phase records whose name matches ``places``.

    ``issue`` keeps one issue's records; ``order`` (a predicate) keeps an order's records.
    """
    spans = {}
    for event in orch_events.events(store, "phase"):
        name = event.get("phase")
        start, end = _time(event.get("phase_started_at")), _time(event.get("at"))
        if not isinstance(name, str) or start is None or end is None or not places(name.lower()):
            continue
        if issue is not None and event.get("issue") != issue:
            continue
        if order is not None and not order(event):
            continue
        spans[(event.get("issue"), name, event.get("phase_started_at"))] = (start, end)
    return list(spans.values())


def covered(spans, low, high):
    """Minutes of [low, high] inside the union of ``spans``."""
    pieces = sorted((max(a, low), min(b, high)) for a, b in spans if min(b, high) > max(a, low))
    minutes, reach = 0.0, low
    for a, b in pieces:
        a = max(a, reach)
        if b > a:
            minutes += (b - a).total_seconds() / 60
            reach = b
    return minutes


def recorded(spans):
    """Minutes the records claim, overlaps counted twice: a doubled record shows here."""
    return sum((b - a).total_seconds() / 60 for a, b in spans)


def names(store, issue=None):
    return sorted({(event.get("issue"), event.get("phase")) for event in orch_events.events(store, "phase")
                   if issue is None or event.get("issue") == issue})


def assert_named_wait(store, places, label, low, high, *, issue=None, order=None):
    span = (high - low).total_seconds() / 60
    assert span > 2 * SLACK, f"Fixture: the {label} wait must be long ({span:.1f} min)"
    spans = phase_spans(store, places, issue=issue, order=order)
    got = covered(spans, low, high)
    assert got >= span - SLACK, (
        f"{label}: phase records naming it cover {got:.1f} of the {span:.1f} minutes it lasted "
        f"({low.time()}-{high.time()}). Recorded phase names: {names(store, issue)}")
    assert recorded(spans) <= span + SLACK, (
        f"{label}: its phase records claim {recorded(spans):.1f} minutes for a {span:.1f}-minute wait "
        "(a doubled or over-long record)")
    return spans


# ---------------------------------------------------------------- owner queue and batch CI

def release(case, monkeypatch):
    """CI of chosen batch branches reports green; every other batch branch stays pending."""
    done = set()
    pending = case.tracker.pr_acceptance_reports

    def measured(head):
        if case.ci.by_head[head].branch in done:
            case.pending_batch = False
            try:
                return pending(head)
            finally:
                case.pending_batch = True
        return pending(head)

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", measured)

    def finish(train):
        done.add(train.head)
        case.tracker.set_pr_checks(train.number, "pass")

    return finish


def tick(case, times=1, step=TICK):
    for _ in range(times):
        case.clock[0] += step
        support.gate(case)


def test_owner_queue_and_pending_batch_ci_are_named_phases_and_not_unknown(batched, monkeypatch):
    case = batched
    case.pending_batch = True
    finish = release(case, monkeypatch)
    queue_workers(case, [5, 1])
    support.gate(case)
    first, = groups(case)  # Train A of #5 and #1; its CI stays pending.
    queue_workers(case, [4, 2])
    queued = case.clock[0]  # #4 and #2 wait in the owner queue behind the running train.
    tick(case, 4)
    finish(first)
    for _ in range(3):
        tick(case)
        if len(groups(case)) == 2:
            break
    assert len(groups(case)) == 2, "Fixture: the owner builds train B of #4 and #2 after train A lands"
    built = case.clock[0]
    second = groups(case)[-1]
    tick(case, 4)  # Train B's CI is pending for 20 minutes.
    finish(second)
    tick(case)
    facts = {n: fact for (n, _), fact in case.store.owner_merge_facts().items()}
    assert {5, 1, 4, 2} <= set(facts), f"Fixture: both trains land: {sorted(facts)}"

    for n in (4, 2):
        landed = datetime.fromisoformat(facts[n]["merged_at"])
        assert_named_wait(case.store, _queue, f"#{n} in the merge-owner queue", queued, built, issue=n)
        assert_named_wait(case.store, _batch_ci, f"#{n} in pending batch CI", built, landed, issue=n)
        early = covered(phase_spans(case.store, _batch_ci, issue=n), queued, built - timedelta(minutes=SLACK))
        assert early <= 1, (
            f"#{n}: the queue wait before its train existed is named batch CI for {early:.1f} minutes; "
            "each wait must name its own place")

    from foragent import orch_scorecard

    split = orch_scorecard.collect(case.store, 2, case.config.lanes)["window"]["today"]["approval_to_merge"]
    parts = split["parts"]
    assert split["total_minutes"] >= 100, f"Fixture: four issues wait about 25-50 minutes each: {split}"
    assert sum(parts.values()) == pytest.approx(split["total_minutes"])
    assert parts["unknown"] <= 2, (
        f"Owner-queue and batch-CI minutes must be counted under a named part, not unknown: {parts}")


# ---------------------------------------------------------------- gate tick gaps

def test_a_gap_between_gate_ticks_while_an_issue_waits_is_a_tick_gap(batched):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    assert len(groups(case)) == 1, "Fixture: #5 and #1 wait on a train whose CI is pending"
    tick(case, 3)
    before = case.clock[0]
    tick(case, step=timedelta(minutes=40))  # No gate tick ran for 40 minutes.
    after = case.clock[0]
    tick(case, 3)
    assert not case.store.owner_merge_facts(), "Fixture: the issues still wait"

    for n in (5, 1):
        spans = phase_spans(case.store, _tick_gap, issue=n)
        got = covered(spans, before, after)
        assert got >= 40 - SLACK, (
            f"#{n}: the 40 minutes without a gate tick are covered {got:.1f} minutes by tick-gap phase "
            f"records. Recorded phase names: {names(case.store, n)}")
        assert recorded(spans) <= 40 + SLACK, (
            f"#{n}: tick-gap records claim {recorded(spans):.1f} minutes; regular 5-minute ticks are not "
            "gaps and one gap is recorded once")


# ---------------------------------------------------------------- orders: preparation, pending publication

def test_order_waits_in_test_preparation_then_as_a_pending_publication_are_named(owner, monkeypatch):
    from foragent import order_prepare_check

    case = owner
    returned(case, "Tests passed on current main: the zephyr check is green.")
    assert statuses(case)[-1] != "published", "Fixture: the red check returned the order to preparation"
    back = case.clock[0]
    for _ in range(12):  # An hour in test preparation; the gate keeps ticking.
        case.clock[0] += TICK
        owner_passes(case, 1)
    reprepare(case)
    red(case)
    submitted = case.clock[0]
    from test_order_publications import submit

    submit(case)  # Now a pending publication; its red check takes 30 minutes.
    measure = order_prepare_check.run

    def slow(*args, **kwargs):
        case.clock[0] += timedelta(minutes=30)
        return measure(*args, **kwargs)

    monkeypatch.setattr(order_prepare_check, "run", slow)
    owner_passes(case, 1)
    for _ in range(2):
        if statuses(case)[-1] == "published":
            break
        case.clock[0] += TICK
        owner_passes(case, 1)
    assert statuses(case)[-1] == "published", f"Fixture: the order is published: {statuses(case)}"
    published = case.clock[0]

    ids = publication_ids(case) | {case.plan["id"]}

    def this_order(event):
        rest = {key: value for key, value in event.items() if key not in {"phase", "summary", "say", "kind"}}
        return any(text in ids or TITLE in text for text in strings(rest))

    assert_named_wait(case.store, _preparation, "the order in test preparation", back, submitted, order=this_order)
    assert_named_wait(case.store, _publication, "the order as a pending publication", submitted, published,
                      order=this_order)
    late = covered(phase_spans(case.store, _preparation, order=this_order), submitted + timedelta(minutes=1), published)
    assert late <= 1, f"The pending publication is still named test preparation for {late:.1f} minutes"


# ---------------------------------------------------------------- flow records from approval to merge

def test_flow_records_of_a_long_lived_issue_keep_its_first_phase_until_its_merge(flow, capsys):
    base = NOW - timedelta(hours=3)
    _approved(flow, 41, base)
    flow.clock[0] = base
    flow.store.record_flow("ready", issue=41, run_started_at=None, reason="free-slot", outcome="waiting")
    # Many other issues' records are written while #41 waits (more than two old generations).
    for index in range(2400):
        flow.clock[0] = base + timedelta(seconds=1 + 2 * index)
        if index % 2:
            flow.store.record_flow("ready", issue=1000 + index % 300, run_started_at=None,
                                   reason="free-slot", outcome="waiting")
        else:
            flow.store.record_flow("tick", status="ok", duration_seconds=1.0, steps=[])
    started = base + timedelta(minutes=90)
    flow.clock[0] = started
    flow.store.start_run(41, branch="foragent/issue-41", worktree=flow.store.worktree_path(41))
    run = flow.store.get_run(41).started_at
    flow.store.record_flow("ready", issue=41, run_started_at=run, reason=None, outcome="started")
    flow.store.update_run(41, phase="implementing", phase_started_at=started.isoformat())
    flow.clock[0] = started + timedelta(minutes=10)
    flow.store.record_flow("activity", issue=41, run_started_at=run, activity="implementing", cause="initial",
                           started_at=started.isoformat(), finished_at=flow.clock[0].isoformat())
    merged = flow.clock[0]
    flow.store.record_merge(41, merged)
    flow.store.update_run(41, last_progress_at=merged.isoformat())
    flow.store.finish_run(41, outcome="merged")
    flow.store.update_local_issue(41, state="closed", labels=["approved"], merged=True,
                                  merged_at=merged.isoformat())

    own = [row for row in flow.store.flow_records() if row["issue"] == 41]
    first = [row for row in own if row["kind"] == "ready" and row["data"].get("outcome") == "waiting"]
    assert len(first) == 1, (
        f"#41's first record (waiting at approval) must still be retained once at merge time: {own[:3]}")
    assert datetime.fromisoformat(first[0]["at"]) == base

    flow.clock[0] = NOW
    card = _read(flow, capsys)
    split = card["window"]["today"]["approval_to_merge"]
    assert split["total_minutes"] == pytest.approx(100)
    assert split["parts"]["unknown"] == pytest.approx(0, abs=0.1), (
        f"#41's early wait must stay explained at merge time, not unknown: {split['parts']}")
    assert split["parts"]["waste"] == pytest.approx(90), split["parts"]
    assert split["parts"]["work"] == pytest.approx(10), split["parts"]
