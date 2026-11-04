"""Shared offline helpers for unit tests."""

from dataclasses import replace
import json
import subprocess
from types import SimpleNamespace

import pytest

from foragent import capacity, executors, orch_architect, scheduler
from foragent.executors import ExecResult

from helpers.review_case import make_case
from helpers.workloads import fake_workloads


def order_children(case, number, children):
    """Published order facts: the existing plan marker and native child links."""
    order_id = f"d99-order-{number}"
    mapping = {f"child-{child}": child for child in children}
    marker = (f"<!-- foragent:plan id={order_id} map={json.dumps(mapping)} "
              "acceptance={} -->")
    case.tracker.add_issue(number, f"Order {number}", f"Finish this order.\n\n{marker}")
    for child in children:
        issue = case.tracker.get_issue(child)
        body = "\n".join(line for line in issue.body.splitlines()
                         if not line.startswith("<!-- foragent:plan-child "))
        case.tracker.edit_body(child, body + (
            f"\n<!-- foragent:plan-child id={order_id} key=child-{child} -->\n"))
        case.tracker.add_sub_issue(number, child)


@pytest.fixture
def review_case(git_history, tmp_path, monkeypatch, fake_workloads):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true", language="en")
    case.store.finish_run(1, "merged")
    case.tracker.close_issue(1)
    case.tracker.remove_labels(1, ["running", "approved"])
    order_children(case, 900, [1])
    case.ctx.config = replace(case.config, roles={"architect": {
        "agent": "fa-architect", "skills": ["fa-architect"],
        "memory": ".claude/agent-memory/fa-architect/", "signals": []}})
    case.launches, case.alive, case.turns = [], set(), []
    case.process_table = fake_workloads
    original = subprocess.Popen

    def launch(argv, *args, **kwargs):
        if "run-architect" not in argv:
            return original(argv, *args, **kwargs)
        child = SimpleNamespace(pid=980001 + len(case.launches), argv=argv, kwargs=kwargs)
        case.launches.append(child)
        case.alive.add(child.pid)
        fake_workloads.add(child.pid, cwd=case.root, start_time=float(child.pid))
        return child

    def execute(**kwargs):
        case.turns.append(kwargs)
        return ExecResult(0, False, "Nothing to change.", kwargs["log_path"])

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(executors, "executor_for", lambda name: SimpleNamespace(run=execute))
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    capacity.write_state(case.store, 2, case.clock[0])
    return case


def complete(case):
    record = orch_architect.read_record(case.store)
    covered = record["started_at"]
    assert orch_architect.run_session(case.root, case.store, case.ctx.config, record["why"],
                                      covered, lambda: case.clock[0]) == 0
    case.alive.remove(record["pid"])
    case.process_table.exit(record["pid"])
    return covered


def add_record(case, number):
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    case.store.finish_run(number, "error", "A new failed run is new evidence too")
