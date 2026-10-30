"""Scheduled review uses the public tick and the existing role worker boundary."""

from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from foragent import capacity, executors, orch_architect, orch_events, processes, scheduler
from foragent.executors import ExecResult
from foragent.state import StateStore
from test_board_status_groups import offline_github  # noqa: F401
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home  # noqa: F401

from test_order_machine_budget import fake_workloads  # noqa: F401


@pytest.fixture
def review_case(git_history, tmp_path, monkeypatch, fake_workloads):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true", language="en")
    case.store.finish_run(1, "merged")
    case.tracker.close_issue(1)
    case.tracker.remove_labels(1, ["running", "approved"])
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


def test_idle_tick_starts_one_project_review_without_role_signals_and_no_new_work_does_not_repeat(review_case):
    case = review_case
    assert scheduler.tick(tick_context(case)).skipped is None
    assert len(case.launches) == 1, "Idle project review must not depend on orchestrator judgment or signals"
    assert "run-architect" in case.launches[0].argv
    assert scheduler.tick(tick_context(case)).skipped is None
    assert len(case.launches) == 1, "A live role session must not be duplicated"
    complete(case)
    case.clock[0] += timedelta(days=2)
    assert scheduler.tick(tick_context(case)).skipped is None
    assert len(case.launches) == 1, "A daily timer without new merges or run records must do no work"


def test_busy_project_reviews_new_records_at_daily_boundary_without_signals(review_case):
    case = review_case
    # The prior successful review is older than the new evidence; all lanes stay busy.
    at = case.clock[0]
    orch_events.append_event(case.store, at, None, "architect-done", "Nothing to change.",
                             started_at=at.isoformat(), covered_until=at.isoformat(), exit_code=0)
    case.clock[0] += timedelta(hours=1)
    add_record(case, 7)
    case.tracker.add_issue(8, "Busy lane", case.tracker.get_issue(1).body, labels=["approved", "running"])
    case.store.start_run(8, branch="busy", worktree=case.store.worktree_path(8), pid=980099)
    case.alive.add(980099)
    case.process_table.add(980099, cwd=case.root, start_time=980099.)
    monkey_config = replace(case.ctx.config, stall_minutes=10000, max_runtime_minutes=10000)
    case.ctx.config = monkey_config
    case.clock[0] = at + timedelta(hours=23, minutes=59)
    assert scheduler.tick(tick_context(case)).skipped is None
    assert case.launches == []
    case.clock[0] += timedelta(minutes=1)
    assert scheduler.tick(tick_context(case)).skipped is None
    assert len(case.launches) == 1, "Busy lanes must not postpone a project review indefinitely"


def test_success_watermark_covers_session_start_so_records_arriving_during_review_remain_due(review_case):
    case = review_case
    assert scheduler.tick(tick_context(case)).skipped is None
    assert len(case.launches) == 1
    started = case.clock[0]
    case.clock[0] += timedelta(minutes=1)
    add_record(case, 7)
    case.clock[0] += timedelta(minutes=1)
    covered = complete(case)
    events = [json.loads(line) for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()]
    done = [event for event in events if event["kind"] == "architect-done"]
    assert len(done) == 1 and done[0]["covered_until"] == started.isoformat() == covered
    assert scheduler.tick(tick_context(case)).skipped is None
    assert len(case.launches) == 2, "Finishing a review must not swallow evidence written after its input cutoff"




def test_due_review_uses_only_a_slot_left_after_ready_issues(review_case):
    case = review_case
    case.tracker.add_issue(2, "Approved work first", case.tracker.get_issue(1).body)
    case.tracker.record_label(2, "approved", "human")
    capacity.write_state(case.store, 1, case.clock[0])
    launched = []
    context = tick_context(case)

    def launch(number):
        launched.append(number)
        case.alive.add(980080)
        case.process_table.add(980080, cwd=case.root, start_time=980080.)
        return 980080

    context.launcher = launch
    assert scheduler.tick(context).skipped is None
    assert launched == [2] and case.launches == [], "Scheduled reviews must yield admission to ready issues"
    case.store.finish_run(2, "merged")
    case.tracker.close_issue(2)
    case.tracker.remove_labels(2, ["running", "approved"])
    case.alive.remove(980080)
    case.process_table.exit(980080)
    assert scheduler.tick(context).skipped is None
    assert len(case.launches) == 1, "A full-budget deferral must leave the project review due"


def test_project_review_instructions_keep_existing_sections_and_proposal_route():
    root = Path(__file__).resolve().parents[2]
    skill = (root / "skills/fa-architect/SKILL.md").read_text()
    sections = ["Measure first", "Proposals", "Refactoring", "Splitting a module", "Memory", "References"]
    assert all(f"## {section}\n" in skill for section in sections)
    assert "## Project review\n" in skill
    for fact in ("spend", "scope drift", "since", "size", "Files"):
        assert fact.lower() in skill.lower()
    assert "fa orch request --from architect" in skill


@pytest.mark.parametrize("hold", ["switch", "plan", "no_start"])
def test_scheduled_roles_share_switch_plan_pause_and_no_start(review_case, hold):
    from foragent import orch_inbox, orch_plan
    case = review_case
    assert getattr(case.ctx.config, "scheduled_roles", None) is True, "Scheduled roles need one default-on switch"
    roles = dict(case.ctx.config.roles)
    roles["orchestrator"] = {"agent": "fa-orchestrator", "skills": ["fa-orchestrator"],
                             "memory": ".claude/agent-memory/fa-orchestrator/", "signals": []}
    case.ctx.config = replace(case.ctx.config, roles=roles)
    if hold == "switch":
        case.ctx.config = replace(case.ctx.config, scheduled_roles=False)
    elif hold == "plan":
        orch_plan.pause(case.store, "main", case.clock[0] + timedelta(days=30), "Project drain", "human")
    assert scheduler.tick(tick_context(case, start_runs=hold != "no_start")).skipped is None
    assert case.launches == []
    assert not [r for r in orch_inbox.requests(case.store) if r.get("kind") == "retrospective"]
    if hold == "switch":
        case.ctx.config = replace(case.ctx.config, scheduled_roles=True)
    elif hold == "plan":
        orch_plan.resume(case.store, "main", "human")
    assert scheduler.tick(tick_context(case)).skipped is None
    assert len(case.launches) == 1
    assert len([r for r in orch_inbox.requests(case.store) if r.get("kind") == "retrospective"]) == 1


def test_failed_scheduled_review_backs_off_for_an_hour_without_advancing_success(review_case, monkeypatch):
    case = review_case
    scheduler.tick(tick_context(case))
    assert len(case.launches) == 1
    record = orch_architect.read_record(case.store)
    monkeypatch.setattr(executors, "executor_for", lambda _: SimpleNamespace(
        run=lambda **kw: ExecResult(7, False, "offline quota failure", kw["log_path"])))
    assert orch_architect.run_session(case.root, case.store, case.ctx.config, record["why"],
                                     record["started_at"], lambda: case.clock[0]) == 7
    case.process_table.exit(record["pid"])
    case.alive.remove(record["pid"])
    scheduler.tick(tick_context(case))
    case.clock[0] += timedelta(minutes=59)
    scheduler.tick(tick_context(case))
    assert len(case.launches) == 1, "Failed role must not relaunch every tick"
    case.clock[0] += timedelta(minutes=1)
    scheduler.tick(tick_context(case))
    assert len(case.launches) == 2, "Failed review remains due after backoff"


def test_schedule_switch_loads_from_settings_and_documents_decision(tmp_path):
    from foragent.config import ConfigError, load_config
    from foragent import settings
    (tmp_path / ".foragent.yml").write_text("repo: offline/project\nscheduled_roles: false\n")
    config = load_config(tmp_path, global_path=tmp_path / "absent.yml")
    assert getattr(config, "scheduled_roles", None) is False
    assert settings.BY_KEY["scheduled_roles"].default is True
    (tmp_path / ".foragent.yml").write_text("scheduled_roles: sometimes\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path, global_path=tmp_path / "absent.yml")
    root = Path(__file__).resolve().parents[2]
    assert "scheduled_roles" in (root / "docs/gate.md").read_text()
    decisions = (root / "docs/decisions.md").read_text()
    assert "2026-10-30" in decisions and "5-4" in decisions
