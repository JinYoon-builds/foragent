"""#1469: direct worsening alarms and role calls even with full lane slots.

Exercise the CLI and its existing worker/executor boundary. Facts are measured
input, not a new signal schema. Never seed suppression state or call alarm
helpers. Worker names, state filenames and comparison structures are not locks.
"""

from copy import deepcopy
from datetime import timedelta
import json
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from test_order_machine_budget import fake_workloads  # noqa: F401
from test_role_source import DEFAULTS, role_entry, write_role_snapshot
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_tick_failure_alert import inbox, run_tick, tick_case  # noqa: F401


RESULT = "Architect result: investigated the queue; simplify the blocking boundary."


@pytest.fixture
def alarm_case(tick_case, monkeypatch, fake_workloads):  # noqa: F811
    from foragent import capacity, executors, orch_facts, orch_facts_resources, processes, scheduler
    from foragent.executors import ExecResult

    case = tick_case
    config_path = case.root / ".foragent.yml"
    config = yaml.safe_load(config_path.read_text())
    config.update(scheduled_roles=False, language="en")
    config_path.write_text(yaml.safe_dump(config))
    roles = {name: role_entry(name) for name in DEFAULTS}
    roles["architect"]["signals"] = [{"paths": [], "kinds": ["lane_concentration"]}]
    write_role_snapshot(case.root, roles)
    case.measured = {
        "kinds": ["lane_concentration"],
        "kind_numbers": {"lane_concentration": {
            "runs": 18, "lane": "roles", "lane_runs": 16,
            "idle_lanes": ["order", "view"],
        }},
        "binding": None,
    }
    case.launches, case.turns = [], []
    case.measure_real_facts = False
    case.measure_real_resources = False
    real_facts = orch_facts.collect

    def facts(*args, **kwargs):
        if case.measure_real_facts:
            return real_facts(*args, **kwargs)
        if case.measure_real_resources:
            snapshot = real_facts(*args, **kwargs)
            snapshot.update({key: deepcopy(case.measured[key]) for key in ("kinds", "kind_numbers")})
            return snapshot
        return {"at": case.clock[0].isoformat(), **deepcopy(case.measured)}

    monkeypatch.setattr(orch_facts, "collect", facts)
    monkeypatch.setattr(orch_facts_resources, "read_process_identity", processes.read_process_identity)
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.5))
    capacity.write_state(case.store, 2, case.clock[0])
    original = subprocess.Popen

    def launch(argv, *args, **kwargs):
        # The public worker command supplies its own child arguments. Do not
        # prescribe the private verb, flags, state record, or alarm helper.
        if len(argv) >= 3 and list(argv[1:3]) == ["-m", "foragent.cli"]:
            child = SimpleNamespace(args=list(argv), kwargs=kwargs,
                                    pid=980500 + len(case.launches))
            case.launches.append(child)
            fake_workloads.add(child.pid, cwd=case.root, start_time=float(child.pid))
            return child
        return original(argv, *args, **kwargs)

    def execute(**kwargs):
        case.turns.append(kwargs)
        return ExecResult(0, False, RESULT, kwargs["log_path"])

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(executors, "executor_for", lambda _: SimpleNamespace(run=execute))
    case.process_table = fake_workloads
    return case


def tick(case, capsys):
    from foragent.state import StateStore

    case.clock[0] += timedelta(minutes=5)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    capsys.readouterr()
    code = cli_exit(case.main, ["gate", "tick", "--json", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, output.err
    assert case.pushes == [], "The gate and architect deliver through the orchestrator"


def finish(case, capsys):
    child = case.launches[-1]
    assert child.kwargs.get("start_new_session") is True
    capsys.readouterr()
    assert cli_exit(case.main, child.args[3:]) == 0
    case.process_table.exit(child.pid)
    assert len(case.turns) == len(case.launches), "Each launched worker must execute one architect turn"
    rows = inbox(case, capsys)
    assert any(RESULT in json.dumps(row) for row in rows), (
        "The orchestrator must receive the architect's actual result"
    )
    assert not [row for row in rows if row.get("from") == "gate"
                and row.get("role") == "architect" and row.get("status") != "done"], (
        "The gate must not leave the orchestrator a request to decide whether to call"
    )


def defer(case, capsys):
    # --no-start records an alarm without starting any worker. It supplies a
    # pre-existing answered signal through the current public CLI, not state.
    run_tick(case, capsys)
    rows = [row for row in inbox(case, capsys) if row.get("role") == "architect"]
    assert len(rows) == 1
    capsys.readouterr()
    assert cli_exit(case.main, ["orch", "signal-answer", rows[0]["id"], "not-now",
                               "--why", "Already investigated", "--repo-root", str(case.root)]) == 0
    return rows[0]["id"]


@pytest.mark.parametrize("interval", [10, 20], ids=["10-minute-rate", "20-minute-rate"])
def test_stall_calls_architect_without_an_orchestrator_answer(alarm_case, capsys, interval):
    case = alarm_case
    case.measure_real_facts = True
    # Three merges at each measured interval establish the rate without a
    # hard-coded minute threshold. An unapproved queue item cannot start.
    start = case.clock[0]
    age = 3 * interval - 15
    for number, minutes in ((81, age + 2 * interval), (82, age + interval), (83, age)):
        case.clock[0] = start - timedelta(minutes=minutes)
        case.store.start_run(number, branch=f"example-{number}",
                             worktree=case.store.worktree_path(number))
        case.store.finish_run(number, "merged")
    case.clock[0] = start
    tick(case, capsys)
    assert case.launches == [], "An empty queue must not alarm even when merges stop"
    case.tracker.add_issue(99, "Queue work", case.tracker.get_issue(1).body)
    tick(case, capsys)
    assert case.launches == [], "Allow the recent merge interval before declaring a stall"
    tick(case, capsys)
    assert len(case.launches) == 1, "A queued stall must call the architect without signal-answer"
    assert case.turns == [], "The gate must return while the independent architect is running"
    tick(case, capsys)
    assert len(case.launches) == 1, "A running dive must not be duplicated"
    finish(case, capsys)


@pytest.mark.parametrize("caller,role", [
    ("gate", "architect"),
    ("architect", "architect"),
    ("role", "architect"),
    ("role", "docs"),
], ids=["gate-alarm", "explicit-architect", "explicit-role-architect", "explicit-role-docs"])
def test_full_lane_slots_do_not_refuse_or_delay_a_role_call(alarm_case, monkeypatch, capsys, caller, role):
    from foragent import capacity
    from foragent.state import StateStore

    case = alarm_case
    case.measure_real_resources = True
    roles = {name: role_entry(name) for name in DEFAULTS}
    roles[role]["signals"] = [{"paths": [], "kinds": ["lane_concentration"]}]
    write_role_snapshot(case.root, roles)
    # Actual run records and live PIDs fill the shared budget. Only the signal's
    # measured numbers are injected; admission and resource facts stay real.
    (case.store.root.parent / "machine.yml").write_text("max_parallel: 4\n")
    capacity.write_state(case.store, 4, case.clock[0])
    lanes = StateStore(case.store.root.parent / "busy-lanes", now=lambda: case.clock[0])
    pids = []
    for number in range(201, 205):
        pid = 981000 + number
        pids.append(pid)
        case.process_table.add(pid, cwd=case.root, start_time=float(pid))
        lanes.start_run(number, branch=f"lane-{number}", worktree=lanes.worktree_path(number),
                        pid=pid, lane=f"lane-{number}")

    def public(command):
        capsys.readouterr()
        assert cli_exit(case.main, ["orch", command, "--json", "--repo-root", str(case.root)]) == 0
        return json.loads(capsys.readouterr().out)

    work = public("facts")["resources"]["parallelizable_work"]
    assert work["machine_runs"] == work["machine_cap"] == 4
    assert work["free_run_slots"] == 0
    assert public("roles")["roles"][role]["signals"][0]["met"] is True

    def waited(_):
        pytest.fail("A role call must start without waiting for a lane run slot")

    monkeypatch.setattr(capacity.time, "sleep", waited)
    if caller == "gate":
        tick(case, capsys)
    else:
        command = ["orch", "architect"] if caller == "architect" else ["orch", "role", role]
        capsys.readouterr()
        code = cli_exit(case.main, [*command, "--why", "Investigate the met signal while lanes run",
                                   "--repo-root", str(case.root)])
        output = capsys.readouterr()
        assert code == 0, output.err
    assert len(case.launches) == 1, "Full lane slots must still allow the role session to start"
    assert case.turns == [], "Return while the independent session is running"
    assert len(lanes.active_runs()) == 4 and all(pid in case.process_table.live for pid in pids)
    if caller == "gate":
        finish(case, capsys)
    else:
        child = case.launches[0]
        assert child.kwargs.get("start_new_session") is True
        assert cli_exit(case.main, child.args[3:]) == 0
        case.process_table.exit(child.pid)
        assert len(case.turns) == 1
    assert roles[role]["memory"] in case.turns[0]["prompt"], "Execute with the requested role's context"
    assert len(lanes.active_runs()) == 4 and all(pid in case.process_table.live for pid in pids)
    assert case.pushes == []


@pytest.mark.parametrize("kind,numbers,worse", [
    ("repeated_design_holds", {"holds_by_issue": {"#1004": 3}},
     {"holds_by_issue": {"#1004": 18}}),
    ("lane_concentration", {"runs": 18, "lane": "roles", "lane_runs": 16,
                            "idle_lanes": ["order", "view"]},
     {"runs": 19, "lane": "roles", "lane_runs": 17,
      "idle_lanes": ["order", "view"]}),
], ids=["holds", "unprojected-numbers"])
def test_worse_number_after_answer_calls_again(alarm_case, capsys, kind, numbers, worse):
    case = alarm_case
    roles = {name: role_entry(name) for name in DEFAULTS}
    roles["architect"]["signals"] = [{"paths": [], "kinds": [kind]}]
    write_role_snapshot(case.root, roles)
    case.measured.update(kinds=[kind], kind_numbers={kind: deepcopy(numbers)})
    defer(case, capsys)
    tick(case, capsys)
    assert case.launches == [], "An answer covers the same numbers"
    case.measured["kind_numbers"][kind] = deepcopy(worse)
    tick(case, capsys)
    assert len(case.launches) == 1, "A worse number must override the earlier not-now"
    finish(case, capsys)
    tick(case, capsys)
    assert len(case.launches) == 1, "The architect's answer covers its measured numbers"


def test_same_or_better_numbers_stay_quiet_across_restarts(alarm_case, capsys):
    case = alarm_case
    defer(case, capsys)
    for hours, runs, lane_runs in ((0, 18, 16), (13, 17, 15), (48, 18, 16), (0, 16, 14)):
        case.clock[0] += timedelta(hours=hours)
        case.measured["kind_numbers"]["lane_concentration"].update(runs=runs, lane_runs=lane_runs)
        # Reorder a set without changing its members or its size.
        case.measured["kind_numbers"]["lane_concentration"]["idle_lanes"].reverse()
        tick(case, capsys)
        assert case.launches == [], "Same or better numbers must stay quiet, including recovery to the answer"
        run_tick(case, capsys)
        assert case.launches == [], "--no-start must preserve suppression without launching a worker"
    rows = [row for row in inbox(case, capsys) if row.get("role") == "architect"]
    assert len(rows) == 1 and rows[0]["status"] == "done", (
        "Numeric improvement must not make another request for the same cause"
    )


def test_comparison_uses_numbers_at_answer_instead_of_the_original_raise(alarm_case, capsys):
    case = alarm_case
    run_tick(case, capsys)
    [row] = [row for row in inbox(case, capsys) if row.get("role") == "architect"]
    case.measured["kind_numbers"]["lane_concentration"].update(runs=16, lane_runs=14)
    capsys.readouterr()
    assert cli_exit(case.main, ["orch", "signal-answer", row["id"], "not-now", "--why",
                               "Answer considers the current improved numbers",
                               "--repo-root", str(case.root)]) == 0
    tick(case, capsys)
    assert case.launches == []
    case.measured["kind_numbers"]["lane_concentration"].update(runs=17, lane_runs=15)
    tick(case, capsys)
    assert len(case.launches) == 1, "17 is worse than the answer's 16, even though the raise saw 18"
    finish(case, capsys)
