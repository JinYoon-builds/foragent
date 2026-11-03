"""Locked acceptance checks for signal-refire; no implementation here.

Replays the 2026-11-03 hot_overlap_files alarms r425, r434 and r438: the gate
called the architect three times in 40 minutes for one 24-hour window whose
counts only drifted (a file reaching the 3-run threshold, counts +1 or +2).
Each extra call cost an architect session and two inbox items.

Only the measured facts are faked (the alarm_case fixture of the gate-alarm
test); ticks, answers and the inbox go through the public `fa` CLI. No
suppression state is seeded. Worker names, state files and comparison
structures are not locks.

Break-it: re-calling on window drift, muting the kind, never calling after a
material change, or calling twice for one change must each fail a check here.
"""

from copy import deepcopy
from datetime import timedelta

import pytest

from test_architect_gate_alarm import alarm_case, defer, finish, tick  # noqa: F401
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_role_source import DEFAULTS, role_entry, write_role_snapshot
from test_skills_autosync import isolated_home  # noqa: F401
from test_tick_failure_alert import inbox, run_tick, tick_case  # noqa: F401


KIND = "hot_overlap_files"
# Measured runs_by_file of r425 (raised 19:03Z, called).
R425 = {
    "src/foragent/ci_mode.py": 3, "src/foragent/cli.py": 4, "src/foragent/commands/order.py": 5,
    "src/foragent/config.py": 4, "src/foragent/config_validation.py": 4, "src/foragent/orch.py": 6,
    "src/foragent/orch_facts.py": 4, "src/foragent/orch_facts_kinds.py": 3,
    "src/foragent/orch_role_raise.py": 4, "src/foragent/order.py": 3, "src/foragent/presence.py": 3,
    "src/foragent/run_publish.py": 3, "src/foragent/run_publish_flow.py": 15,
    "src/foragent/run_publish_reviewed.py": 14, "src/foragent/run_review.py": 6,
    "src/foragent/runner.py": 6, "src/foragent/scheduler.py": 10, "src/foragent/tracker.py": 5,
    "tests/test_config.py": 4,
}
# r434 (19:25Z): one file reached the threshold, three counts rose by 1-2.
R434 = {**R425, "src/foragent/run_review.py": 7, "src/foragent/scheduler.py": 12,
        "src/foragent/tracker.py": 6, "tests/runner_support.py": 3}
# r438 (19:43Z): one more file reached the threshold, two counts rose by 1.
R438 = {**R434, "src/foragent/scheduler.py": 13, "src/foragent/tracker.py": 7,
        "src/foragent/scheduler_checks.py": 3}


def hot_files(case, runs_by_file):
    roles = {name: role_entry(name) for name in DEFAULTS}
    roles["architect"]["signals"] = [{"paths": [], "kinds": [KIND]}]
    write_role_snapshot(case.root, roles)
    case.measured.update(kinds=[KIND], kind_numbers={KIND: {"runs_by_file": deepcopy(runs_by_file)}})


def measure(case, runs_by_file, minutes=0):
    case.clock[0] += timedelta(minutes=minutes)
    case.measured["kind_numbers"][KIND] = {"runs_by_file": deepcopy(runs_by_file)}


def architect_rows(case, capsys):
    return [row for row in inbox(case, capsys) if row.get("role") == "architect" and row.get("from") == "gate"]


def call_once(case, capsys):
    """r425: the first met signal calls the architect once, and the same facts never call again."""
    tick(case, capsys)
    assert len(case.launches) == 1, "A met, never-answered signal must call the architect"
    tick(case, capsys)
    assert len(case.launches) == 1, "The same facts must not start a second session"
    finish(case, capsys)
    tick(case, capsys)
    assert len(case.launches) == 1, "The finished dive covers its facts"
    assert len(architect_rows(case, capsys)) == 1


def stays_quiet(case, capsys, rows_before):
    for numbers, minutes in ((R434, 20), (R438, 18)):
        measure(case, numbers, minutes)
        tick(case, capsys)
        assert len(case.launches) == rows_before["launches"], (
            "Drift of the same 24-hour window (a file reaching the threshold, counts +1/+2) "
            "must not call the architect again"
        )
        run_tick(case, capsys)
        rows = architect_rows(case, capsys)
        assert len(rows) == rows_before["rows"] and all(row["status"] == "done" for row in rows), (
            "Window drift must not raise another request for the orchestrator to answer"
        )


def test_window_drift_after_a_called_dive_does_not_call_again(alarm_case, capsys):
    case = alarm_case
    hot_files(case, R425)
    call_once(case, capsys)
    stays_quiet(case, capsys, {"launches": 1, "rows": 1})


def test_window_drift_after_not_now_is_not_raised_again(alarm_case, capsys):
    case = alarm_case
    hot_files(case, R425)
    defer(case, capsys)  # --no-start raise, then `fa orch signal-answer <id> not-now`.
    tick(case, capsys)
    assert case.launches == [], "A not-now answer covers the same facts"
    stays_quiet(case, capsys, {"launches": 0, "rows": 1})


@pytest.mark.parametrize("hours,changed", [
    (0, {"src/foragent/run_publish_flow.py": 30}),
    (25, {"src/foragent/run_publish_merge.py": 3}),
], ids=["a-hot-file-doubles", "a-new-file-after-the-window"])
def test_a_material_change_calls_again_exactly_once(alarm_case, capsys, hours, changed):
    case = alarm_case
    hot_files(case, R425)
    call_once(case, capsys)
    # Drift first, so this case cannot pass where drift alone re-fires (no passing guard on main).
    stays_quiet(case, capsys, {"launches": 1, "rows": 1})
    measure(case, {**R425, **changed}, minutes=60 * hours)
    tick(case, capsys)
    assert len(case.launches) == 2, "A material change must call the architect again"
    tick(case, capsys)
    assert len(case.launches) == 2, "One change calls once"
    finish(case, capsys)
    tick(case, capsys)
    run_tick(case, capsys)
    assert len(case.launches) == 2 and len(architect_rows(case, capsys)) == 2
