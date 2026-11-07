"""Acceptance checks for concentration-rise-needs-a-start (architect r1093).

A lane_concentration share that rises only because older runs left the 6-hour
window (r1092: 13/25 -> 13/23, no new start in the busy lane) is not raised
again. Every test checks the aged-out ticks first; the tests that go on to a
new busy-lane start (which still raises, as today) only reach that part after
the aged-out tick has been shown to create no new request.

Entry points: `fa gate tick --no-start` (inbox re-raise), `fa gate tick`
(the gate's direct architect call), `fa orch signal-answer` and
`fa orch inbox --json`. Facts are either the measured `fa orch facts` input
(faked like the locked role-signal tests) or the real run history, where runs
really age out of the window as the clock moves.

Break-it: treating any share rise as worse (main today), ignoring a new busy
lane start, or raising twice must fail these checks.
"""

from copy import deepcopy
from datetime import timedelta

import pytest
import yaml

from test_architect_gate_alarm import alarm_case, defer, tick  # noqa: F401
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_role_source import DEFAULTS, role_entry, write_role_snapshot
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_tick_failure_alert import inbox, run_tick, tick_case  # noqa: F401


KIND = "lane_concentration"
SIGNAL = {"paths": [], "kinds": [KIND]}
# r1084 then r1092: same 13 busy-lane runs, two older runs aged out.
RAISED = {"runs": 25, "lane": "main", "lane_runs": 13, "idle_lanes": ["order", "view"]}
AGED_OUT = {"runs": 23, "lane": "main", "lane_runs": 13, "idle_lanes": ["order", "view"]}
AGED_MORE = {"runs": 21, "lane": "main", "lane_runs": 13, "idle_lanes": ["view", "order"]}
NEW_START = {"runs": 22, "lane": "main", "lane_runs": 14, "idle_lanes": ["order", "view"]}


def concentration_rows(case, capsys):
    return [row for row in inbox(case, capsys)
            if row.get("role") == "architect" and row.get("signal") == SIGNAL]


def answer(case, capsys, request_id, how):
    capsys.readouterr()
    code = cli_exit(case.main, [
        "orch", "signal-answer", request_id, how, "--why",
        "The architect already looked at this lane's concentration",
        "--repo-root", str(case.root),
    ])
    output = capsys.readouterr()
    assert code == 0, f"signal-answer must work: {output.err}"


@pytest.fixture
def measured_case(tick_case, monkeypatch):  # noqa: F811
    from foragent import orch_facts

    case = tick_case
    roles = {name: role_entry(name) for name in DEFAULTS}
    roles["architect"]["signals"] = [deepcopy(SIGNAL)]
    write_role_snapshot(case.root, roles)
    case.numbers = deepcopy(RAISED)

    def measured_facts(*args, **kwargs):
        return {"at": case.clock[0].isoformat(), "kinds": [KIND],
                "kind_numbers": {KIND: deepcopy(case.numbers)}, "binding": None}

    monkeypatch.setattr(orch_facts, "collect", measured_facts)
    return case


@pytest.mark.parametrize("how", ["not-now", "called"])
def test_share_rising_only_by_aging_out_is_not_raised_again(measured_case, capsys, how):
    case = measured_case
    run_tick(case, capsys)
    [first] = concentration_rows(case, capsys)
    assert first["status"] != "done"
    answer(case, capsys, first["id"], how)

    for numbers, advance in ((AGED_OUT, timedelta(minutes=50)), (AGED_MORE, timedelta(hours=2)),
                             (AGED_OUT, timedelta(hours=13))):
        case.numbers = deepcopy(numbers)
        case.clock[0] += advance
        run_tick(case, capsys)
        rows = concentration_rows(case, capsys)
        assert [row["id"] for row in rows] == [first["id"]], (
            f"{how}: share {numbers['lane_runs']}/{numbers['runs']} rose only because older runs "
            "left the window, with no new start in the busy lane; lane_concentration must not be raised again"
        )
        assert rows[0]["status"] == "done"


@pytest.mark.parametrize("how", ["not-now", "called"])
def test_a_new_busy_lane_start_that_raises_the_share_raises_again_once(measured_case, capsys, how):
    case = measured_case
    run_tick(case, capsys)
    [first] = concentration_rows(case, capsys)
    answer(case, capsys, first["id"], how)
    case.numbers = deepcopy(AGED_OUT)
    case.clock[0] += timedelta(minutes=50)
    run_tick(case, capsys)
    rows = concentration_rows(case, capsys)
    assert [(row["id"], row["status"]) for row in rows] == [(first["id"], "done")], (
        f"{how}: before any new start, the aged-out tick (13/25 -> 13/23) must leave only the "
        "answered original request and create no new lane_concentration request"
    )

    case.numbers = deepcopy(NEW_START)
    run_tick(case, capsys)
    rows = concentration_rows(case, capsys)
    added = [row for row in rows if row["id"] != first["id"]]
    assert len(added) == 1, "A new run in the busy lane that raises its share must raise lane_concentration again"
    assert added[0]["status"] != "done"
    run_tick(case, capsys)
    assert len(concentration_rows(case, capsys)) == 2, "The same new start must raise only once"


def test_gate_does_not_call_the_architect_when_only_older_runs_left(alarm_case, capsys):  # noqa: F811
    case = alarm_case
    case.measured["kind_numbers"][KIND] = deepcopy(RAISED)
    defer(case, capsys)
    for numbers in (AGED_OUT, AGED_MORE):
        case.measured["kind_numbers"][KIND] = deepcopy(numbers)
        tick(case, capsys)
        assert case.launches == [], (
            "A share rise without a new start in the busy lane must not call the architect"
        )
        run_tick(case, capsys)
        assert case.launches == []
    rows = [row for row in inbox(case, capsys) if row.get("role") == "architect"]
    assert len(rows) == 1 and rows[0]["status"] == "done", "No new lane_concentration request either"

    case.measured["kind_numbers"][KIND] = deepcopy(NEW_START)
    tick(case, capsys)
    assert len(case.launches) == 1, "A new busy-lane start that raises the share must call the architect as today"


# Real run history: runs age out of the 6-hour window as the gate clock moves.

LANES = {"view": {"paths": ["src/view/**"]}, "order": {"paths": ["src/order/**"]},
         "board": {"paths": ["src/board/**"]}}


@pytest.fixture
def history_case(tick_case):  # noqa: F811
    case = tick_case
    path = case.root / ".foragent.yml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["lanes"] = deepcopy(LANES)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    roles = {name: role_entry(name) for name in DEFAULTS}
    roles["architect"]["signals"] = [deepcopy(SIGNAL)]
    write_role_snapshot(case.root, roles)
    case.next_number = 500
    return case


def started(case, lane, at):
    from foragent.state import StateStore

    now = case.clock[0]
    clock = [at]
    store = StateStore(case.store.root, now=lambda: clock[0])
    number = case.next_number
    case.next_number += 1
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number), lane=lane)
    clock[0] = at + timedelta(minutes=1)
    store.finish_run(number, outcome="checks-failed")
    case.clock[0] = now


def test_real_history_aging_out_stays_quiet_and_a_new_start_raises(history_case, capsys):
    case = history_case
    base = case.clock[0]
    # Two old "view" runs near the window's edge, then 11 more "view" and 13 "main".
    # Within the window at base: 25 runs, main 13 (> half), order and board idle.
    for minutes in (345, 340):
        started(case, "view", base - timedelta(minutes=minutes))
    for index in range(11):
        started(case, "view", base - timedelta(minutes=200 - index))
    for index in range(13):
        started(case, "main", base - timedelta(minutes=120 - index))
    run_tick(case, capsys)  # clock base + 5 min: all 25 still inside 6 h
    rows = concentration_rows(case, capsys)
    assert len(rows) == 1, "Real history of 13/25 must meet lane_concentration once"
    first = rows[0]
    answer(case, capsys, first["id"], "not-now")

    # Move the clock so the two old view runs leave the window: 13/23, no new start.
    case.clock[0] = base + timedelta(minutes=15)
    run_tick(case, capsys)  # base + 20 min
    rows = concentration_rows(case, capsys)
    assert [row["id"] for row in rows] == [first["id"]], (
        "Two older runs leaving the 6-hour window (13/25 -> 13/23) is elapsed time, not a new start; "
        "lane_concentration must not be raised again"
    )

    # A new start in the busy lane: 14/24, share rises again.
    started(case, "main", case.clock[0] - timedelta(minutes=1))
    run_tick(case, capsys)
    rows = concentration_rows(case, capsys)
    added = [row for row in rows if row["id"] != first["id"]]
    assert len(added) == 1 and added[0]["status"] != "done", (
        "A new run in the busy lane that raises its share must raise lane_concentration again"
    )
