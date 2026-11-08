"""Necessary waiting and lane-slot utilization in the four-part split (#2485).

Hand-built records in the shapes the gate writes: ``overlap`` flow records from
overlap_files.note_holds (``run_started_at`` None before a run starts), ``ready`` records
from scheduler_flow, and execution rows as orch_scorecard_records._executions returns them.
"""

from datetime import UTC, datetime, timedelta

import pytest

from foragent import orch_scorecard_four as four, orch_scorecard_waits as waits

T0 = datetime(2026, 11, 7, 9, tzinfo=UTC)
NOW = T0 + timedelta(hours=1)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def execution(number, start, end=None):
    return {"number": number, "started": at(start), "finished": None if end is None else at(end)}


def hold(minute, *, run=None, peer=(2, -5), file="src/a.py", cause=None, issue=1):
    holds = [] if peer is None else [{"file": file, "limit": None if file is None else 1,
                                      **({"cause": cause} if cause else {}),
                                      "peers": [{"issue": peer[0], "run_started_at": at(peer[1]).isoformat()}]}]
    return {"kind": "overlap", "issue": issue, "at": at(minute).isoformat(),
            "run_started_at": None if run is None else at(run).isoformat(), "data": {"holds": holds}}


def ready(minute, reason, outcome="waiting"):
    return {"kind": "ready", "issue": 1, "at": at(minute).isoformat(), "run_started_at": None,
            "data": {"reason": reason, "outcome": outcome}}


def parts(records, executions, *, merged=20, run_start=10):
    """Issue #1 approved at 0, started at ``run_start``, implementing until its merge."""
    phases = [("implementing", (at(run_start), at(merged)), None)]
    held = waits.evidence(records, 1, executions, NOW)
    totals = four.parts(at(0), at(merged), phases, records, [], [at(run_start)], 1, held)
    assert sum(totals.values()) == pytest.approx(merged)
    return {key: round(value, 6) for key, value in totals.items() if key.endswith("waiting")}


def test_an_admission_hold_on_a_running_peer_is_necessary_until_the_run_starts():
    records = [hold(2), ready(2, "file-overlap")]
    totals = parts(records, [execution(2, -5, 30), execution(1, 10, 20)])
    assert totals == {"necessary_waiting": 8, "wasted_waiting": 2}


def test_an_admission_hold_on_a_finished_peer_is_wasted():
    records = [hold(2), ready(2, "file-overlap")]
    totals = parts(records, [execution(2, -5, 1), execution(1, 10, 20)])
    assert totals == {"necessary_waiting": 0, "wasted_waiting": 10}


def test_a_peer_that_finishes_partway_ends_the_necessary_wait():
    records = [hold(2), ready(2, "file-overlap")]
    totals = parts(records, [execution(2, -5, 6), execution(1, 10, 20)])
    assert totals == {"necessary_waiting": 4, "wasted_waiting": 6}


def test_a_hold_names_its_peer_execution_not_just_the_issue():
    # The same issue's later execution is not the one the hold named.
    records = [hold(2), ready(2, "file-overlap")]
    totals = parts(records, [execution(2, -5, 1), execution(2, 3, 30), execution(1, 10, 20)])
    assert totals == {"necessary_waiting": 0, "wasted_waiting": 10}


def test_a_lane_serialized_hold_on_a_running_peer_is_necessary():
    records = [hold(2, file=None, cause="lane-serialized"), ready(2, "file-overlap")]
    totals = parts(records, [execution(2, -5, 30), execution(1, 10, 20)])
    assert totals == {"necessary_waiting": 8, "wasted_waiting": 2}


def test_a_newer_ready_cause_ends_the_admission_wait_and_a_release_ends_the_hold():
    # Quota from 4 to 6, file-overlap again (unchanged hold, not re-recorded) until the release at 8.
    records = [hold(2), ready(2, "file-overlap"), ready(4, "quota"), ready(6, "file-overlap"),
               hold(8, peer=None)]
    totals = parts(records, [execution(2, -5, 30), execution(1, 10, 20)])
    assert totals == {"necessary_waiting": 4, "wasted_waiting": 6}


def test_an_in_run_hold_counts_inside_its_wait_phase_and_ends_with_its_release():
    records = [hold(12, run=10), hold(15, run=10, peer=None)]
    phases = [("implementing", (at(10), at(12)), None), ("overlap-wait", (at(12), at(16)), None),
              ("implementing", (at(16), at(20)), None)]
    executions = [execution(2, -5, 30), execution(1, 10, 20)]
    totals = four.parts(at(0), at(20), phases, records, [], [at(10)], 1,
                        waits.evidence(records, 1, executions, NOW))
    assert totals["necessary_waiting"] == pytest.approx(3)
    assert totals["wasted_waiting"] == pytest.approx(11)  # 0-10 before the run, 15-16 after the release.


def test_another_issues_hold_is_not_this_issues_evidence():
    records = [hold(2, issue=3), ready(2, "file-overlap")]
    totals = parts(records, [execution(2, -5, 30), execution(1, 10, 20)])
    assert totals == {"necessary_waiting": 0, "wasted_waiting": 10}


# ---------------------------------------------------------------- utilization

def test_utilization_is_busy_over_available_slot_time_with_a_budget_change():
    # Budget 1 from -60 to 0, then 2. One run busy -30..30: busy 30 + 30, available 60 + 120.
    budgets = [(at(-60), 1), (at(0), 2)]
    value = waits.utilization([execution(5, -30, 30)], budgets, NOW, 1)[0]
    assert value == pytest.approx(60 / 180)


def test_utilization_rises_with_busy_time_and_caps_busy_slots_at_the_budget():
    budgets = [(at(-60), 1)]
    short = waits.utilization([execution(5, 0, 30)], budgets, NOW, 1)[0]
    long = waits.utilization([execution(5, 0, 60)], budgets, NOW, 1)[0]
    assert (short, long) == pytest.approx((30 / 120, 60 / 120))
    assert waits.utilization([execution(5, -60, 60), execution(6, -60, 60)], budgets, NOW, 1) == [1]


def test_utilization_is_none_without_a_recorded_budget():
    assert waits.utilization([execution(5, 0, 30)], [], NOW, 2) == [None, None]


def test_the_previous_window_has_the_median_daily_utilization():
    days = [{"median": None, "issues": [], "utilization": value} for value in (.5, None, 1)]
    assert four.previous(days)["utilization"] == pytest.approx(.75)
