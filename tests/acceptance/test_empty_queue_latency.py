"""Acceptance test for n20-empty-queue-latency.

A standalone sample is a size s issue approved while no other executable issue of the
repository is queued (approved and ready) or running, measured from approval to its
recorded merge. Queue state at approval comes only from retained local evidence (issue
observations and run records): an approval older than every retained record has an
unknown queue, which is reported as missing evidence, never as an empty queue.

The card exposes, per scorecard window, ``standalone_latency`` =
``{"samples": int, "median_minutes": number | None, "missing_evidence": int}``.
"""

from datetime import timedelta
import os
import re

import pytest

from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli
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
    return case


def _observe(case, number, at, labels, state="open"):
    case.clock[0] = at
    case.store.update_local_issue(number, title=f"Issue {number}", labels=sorted(labels), state=state,
                                  blocked_by=[])


def _approve(case, number, at, size, *, observe=True):
    from foragent.issue import body_hash

    case.clock[0] = at
    body = ("<!-- foragent:v1 -->\n## Goal\nChange the module.\n## Files\n"
            f"- src/issue_{number}.py\n## Out of scope\nOther modules.\n"
            "## Done when\n```sh\ntrue\n```\n")
    case.store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                             approved_by="human", approved_at=at.isoformat())
    labels = {"approved", "ready", f"size:{size}"}
    if observe:
        case.store.update_local_issue(number, title=f"Issue {number}", body=body, labels=sorted(labels),
                                      state="open", blocked_by=[])
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
    # Retained evidence well before every approval below: an unrelated closed issue.
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
    _issue(case, 15, "l", h(16), h(15, 30), h(14, 50))
    _issue(case, 14, "s", h(15), h(14, 20), h(14))
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
    # #20 was approved 10 h ago, but no observation or run record of that time was kept:
    # the first retained record is its run, 20 minutes later. Its queue state is unknown.
    _approve(flow, 20, h(10), "s", observe=False)
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
    _approve(flow, 20, h(10), "s", observe=False)  # No retained record at its approval.
    _start(flow, 20, h(9, 50), "s")
    _merged(flow, 20, h(9, 40), "s")
    # Records exist from 9 h 50 min ago, so #10's empty queue at approval is evidenced.
    _issue(flow, 10, "s", h(6), h(5, 58), h(5, 48))
    flow.clock[0] = NOW
    today = _latency(_read(flow, capsys))
    assert (today["samples"], today["missing_evidence"]) == (1, 1), (
        f"one eligible sample (#10) and one approval with unknown queue (#20), got {today!r}")
    assert today["median_minutes"] == pytest.approx(12), (
        f"the unknown sample must not enter the median: only #10's 12 minutes count, got {today!r}")
