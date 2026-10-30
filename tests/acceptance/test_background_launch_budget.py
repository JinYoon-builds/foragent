"""Noninteractive role process counts once using existing live records."""

from types import SimpleNamespace
from foragent import capacity, executors, orch_architect
from foragent.executors import ExecResult
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_architect_project_schedule import review_case, complete  # noqa: F401

def test_role_call_is_a_machine_run_with_pid_identity_and_child_does_not_double_count(review_case):
    case = review_case
    orch_architect.call(case.root, case.store, case.ctx.config, "project review", case.clock[0])
    assert capacity.machine_runs(case.store, case.clock[0]) == 1
    record = orch_architect.read_record(case.store)

    def during(**kwargs):
        assert capacity.machine_runs(case.store, case.clock[0]) == 1
        return ExecResult(0, False, "Nothing to change.", kwargs["log_path"])

    # Replace the fake executor at the public boundary; no child process starts here.
    from unittest.mock import patch
    with patch.object(executors, "executor_for", return_value=SimpleNamespace(run=during)):
        complete(case)
    assert capacity.machine_runs(case.store, case.clock[0]) == 0

