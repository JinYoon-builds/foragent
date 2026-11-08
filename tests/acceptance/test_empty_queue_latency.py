"""Acceptance test for n20-empty-queue-latency.

A standalone sample is a size s issue approved while no other executable issue of the
repository is queued (approved and ready) or running, measured from approval to its
recorded merge. Queue state at approval comes only from the fact the gate's approval step
(``scheduler_approval._snapshot``, the one writer) appends when it saves the approval
snapshot: the numbers of the other queued or running issues at that moment. An approval
saved without that fact (for example one saved before the writer existed) has an unknown
queue, which is reported as missing evidence, never as an empty queue. Later changes to
issue observations cannot change a past sample.

The card exposes, per scorecard window, ``standalone_latency`` =
``{"samples": int, "median_minutes": number | None, "missing_evidence": int}``.
"""

from datetime import timedelta
import os
import re

import pytest

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, tick_context
from test_orch_scorecard import scorecard_case
from test_scorecard_flow import NOW, _labels, _read
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.fixture
def flow(git_history, tmp_path, monkeypatch):
    """The scorecard-flow world without the watch-only ``time.sleep`` guard.

    The shared ``local_cli`` helper fails any ``time.sleep`` call to prove the initial
    ``orch watch`` emission does not poll. The scorecard legitimately runs local Git reads
    whose subprocess waits may sleep, so this fixture keeps the real GhTracker and the
    GitHub-call guard but leaves ``time.sleep`` alone.
    """
    from foragent import capacity, cli
    from foragent.tracker import GhTracker

    case = scorecard_case(git_history, tmp_path, monkeypatch)
    case.clock[0] = NOW
    case.files_root = tmp_path
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.25))
    monkeypatch.setattr(os, "getloadavg", lambda: (0.25, 0.25, 0.25))
    case.gh_calls = record_gh(monkeypatch)
    case.main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "GhTracker", GhTracker)
    # The shared world's tracker holds a running #1 that is not part of these scenarios.
    first = case.tracker.get_issue(1)
    case.tracker.add_issue(1, first.title, first.body, (), state="closed")
    case.bodies = {}
    return case


def _body(case, number):
    return case.bodies.setdefault(number, (
        "<!-- foragent:v1 -->\n## Goal\nChange the module.\n## Files\n"
        f"- src/issue_{number}.py\n## Out of scope\nOther modules.\n"
        "## Done when\n```sh\ntrue\n```\n"))


def _observe(case, number, at, labels, state="open"):
    """The gate's issue observation, mirrored on the tracker so both sources agree."""
    case.clock[0] = at
    case.tracker.add_issue(number, f"Issue {number}", _body(case, number), sorted(labels), state=state)
    case.store.update_local_issue(number, title=f"Issue {number}", body=_body(case, number),
                                  labels=sorted(labels), state=state, blocked_by=[])


def _approve(case, number, at, size, *, through_gate=True):
    """Approve ``number`` at ``at``; the gate's approval step saves the snapshot and its fact.

    ``through_gate=False`` saves only the approval snapshot, as an approval saved before the
    approval-time queue fact existed: its queue state at approval is unknown.
    """
    from foragent import scheduler
    from foragent.issue import body_hash

    _observe(case, number, at, {"approved", f"size:{size}"})
    case.clock[0] = at
    if through_gate:
        case.tracker.record_label(number, "approved", "human")
        event = case.tracker.label_events(number)[-1]
        report = scheduler.TickReport()
        approval = scheduler._snapshot(tick_context(case, start_runs=False), case.tracker.get_issue(number),
                                       event, "origin/main", report)
        assert approval is not None and approval.approved_at == at.isoformat(), (
            f"the gate's approval step must save #{number}'s snapshot: {report.actions!r}")
    else:
        body = _body(case, number)
        case.store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                                 approved_by="human", approved_at=at.isoformat())
    labels = {"approved", "ready", f"size:{size}"}
    _observe(case, number, at, labels)
    _labels(case, number, [(at, label, "labeled") for label in sorted(labels)])


def _start(case, number, at, size):
    case.clock[0] = at
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    _observe(case, number, at, {"approved", "running", f"size:{size}"})


def _merged(case, number, at, size):
    case.clock[0] = at
    case.store.record_merge(number, at)
    case.store.update_run(number, last_progress_at=at.isoformat())
    case.store.finish_run(number, outcome="merged")
    case.tracker.add_issue(number, f"Issue {number}", _body(case, number), ["approved", f"size:{size}"],
                           state="closed")
    case.store.update_local_issue(number, state="closed", labels=["approved", f"size:{size}"], merged=True,
                                  merged_at=at.isoformat(), blocked_by=[])


def _issue(case, number, size, approved, started, merged):
    _approve(case, number, approved, size)
    _start(case, number, started, size)
    _merged(case, number, merged, size)


def _latency(card, window="today"):
    metrics = card["window"][window]
    assert "standalone_latency" in metrics, f"the {window} window must expose standalone_latency"
    value = metrics["standalone_latency"]
    assert isinstance(value, dict) and {"samples", "median_minutes", "missing_evidence"} <= value.keys(), (
        f"standalone_latency must report samples, median_minutes and missing_evidence, got {value!r}")
    return value


def _lines(text):
    return [line for line in text.splitlines() if "standalone" in line.lower()]


def _seed_mixed_day(case):
    h = lambda hours, minutes=0: NOW - timedelta(hours=hours, minutes=minutes)  # noqa: E731
    # An unrelated closed issue: closed issues are never queued or running.
    _observe(case, 50, h(23, 30), {"size:s"}, state="closed")
    # #13 (size m) waits approved and ready from 22 h ago, runs only from 20 h 10 min ago.
    _approve(case, 13, h(22), "m")
    # #12 (size s) is approved while #13 is queued: not a standalone sample (120 min).
    _approve(case, 12, h(21), "s")
    _start(case, 13, h(20, 10), "m")
    _merged(case, 13, h(20), "m")
    _start(case, 12, h(19, 10), "s")
    _merged(case, 12, h(19), "s")
    # #15 (size l) runs from 15 h 30 min ago; #14 (size s) is approved while it runs (60 min).
    # The gate sees each approval when it happens, so the steps run in time order.
    _approve(case, 15, h(16), "l")
    _start(case, 15, h(15, 30), "l")
    _approve(case, 14, h(15), "s")
    _merged(case, 15, h(14, 50), "l")
    _start(case, 14, h(14, 20), "s")
    _merged(case, 14, h(14), "s")
    # Two standalone size s issues: 12 and 20 minutes from approval to merge.
    _issue(case, 10, "s", h(6), h(5, 58), h(5, 48))
    _issue(case, 11, "s", h(3), h(2, 50), h(2, 40))


def test_two_standalone_small_issues_give_two_samples_and_a_median_of_sixteen(flow, capsys):
    _seed_mixed_day(flow)
    flow.clock[0] = NOW
    today = _latency(_read(flow, capsys))
    assert today["samples"] == 2, (
        "Only #10 and #11 were size s and approved with no other issue queued or running; "
        f"issues approved behind other work or of another size must not count, got {today!r}")
    assert today["median_minutes"] == pytest.approx(16), f"median of 12 and 20 minutes is 16, got {today!r}"
    assert today["missing_evidence"] == 0, f"every approval had retained queue evidence, got {today!r}"


def test_the_human_scorecard_shows_the_standalone_samples_and_median(flow, capsys):
    _seed_mixed_day(flow)
    flow.clock[0] = NOW
    lines = _lines(_read(flow, capsys, human=True))
    assert any(re.search(r"(?<![\d.])2(?![\d.])", line) and re.search(r"(?<![\d.])16(?![\d.])", line)
               for line in lines), f"a standalone latency line must show 2 samples and 16 minutes, got {lines!r}"


def test_a_window_without_eligible_samples_is_unknown_not_zero(flow, capsys):
    _seed_mixed_day(flow)
    flow.clock[0] = NOW
    previous = _latency(_read(flow, capsys), "previous")
    assert previous["samples"] == 0 and previous["missing_evidence"] == 0, (
        f"nothing was approved or merged in the previous window, got {previous!r}")
    assert previous["median_minutes"] is None, (
        f"a window without completed standalone samples has an unknown latency, not 0: {previous!r}")


def test_an_approval_without_retained_queue_evidence_is_missing_coverage(flow, capsys):
    h = lambda hours, minutes=0: NOW - timedelta(hours=hours, minutes=minutes)  # noqa: E731
    # #20's approval was saved 10 h ago without the gate's approval-time queue fact. The
    # store observed it alone in an empty queue, but only the fact may decide: unknown.
    _approve(flow, 20, h(10), "s", through_gate=False)
    _start(flow, 20, h(9, 50), "s")
    _merged(flow, 20, h(9, 40), "s")
    flow.clock[0] = NOW
    today = _latency(_read(flow, capsys))
    assert today["samples"] == 0, f"an approval with unknown queue state is not an eligible sample: {today!r}"
    assert today["missing_evidence"] == 1, f"the unknown queue state must be counted as missing evidence: {today!r}"
    assert today["median_minutes"] is None, f"with no eligible sample the latency is unknown, not 0: {today!r}"
    lines = _lines(_read(flow, capsys, human=True))
    assert lines and not any(re.search(r"(?<![\d.])20(?![\d.])", line) for line in lines), (
        f"the human card must show a standalone line without using #20's 20 minutes, got {lines!r}")
    assert any(re.search(r"unknown|(?<![\w.])-(?![\w.])", line, re.IGNORECASE) for line in lines), (
        f"the human card must show the standalone latency as unknown, got {lines!r}")


def test_missing_evidence_is_counted_apart_from_eligible_samples(flow, capsys):
    h = lambda hours, minutes=0: NOW - timedelta(hours=hours, minutes=minutes)  # noqa: E731
    _approve(flow, 20, h(10), "s", through_gate=False)  # No approval-time queue fact.
    _start(flow, 20, h(9, 50), "s")
    _merged(flow, 20, h(9, 40), "s")
    # The gate recorded #10's empty queue at approval (#20 had already merged).
    _issue(flow, 10, "s", h(6), h(5, 58), h(5, 48))
    flow.clock[0] = NOW
    today = _latency(_read(flow, capsys))
    assert (today["samples"], today["missing_evidence"]) == (1, 1), (
        f"one eligible sample (#10) and one approval with unknown queue (#20), got {today!r}")
    assert today["median_minutes"] == pytest.approx(12), (
        f"the unknown sample must not enter the median: only #10's 12 minutes count, got {today!r}")


def test_later_changes_to_observations_do_not_change_a_past_sample(flow, capsys):
    _seed_mixed_day(flow)
    # Later reads lose the queued #13 and the running #15: their rows, with their cached
    # label history, leave the retained observations. The facts recorded
    # when #12 and #14 were approved still say they waited behind other work.
    for number in (13, 15):
        flow.clock[0] = NOW - timedelta(minutes=30)
        flow.store.ingest_absence(number, NOW - timedelta(minutes=30))
        assert all(row["number"] != number for row in flow.store.local_issues())
    flow.clock[0] = NOW
    today = _latency(_read(flow, capsys))
    assert (today["samples"], today["median_minutes"], today["missing_evidence"]) == (2, pytest.approx(16), 0), (
        "the queue state at approval is the gate's recorded fact; later observation changes must not "
        f"turn #12 or #14 into standalone samples, got {today!r}")
