"""Boundaries of lane test drafts (#1729) beyond the locked acceptance file."""

from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_lane_test_drafts import (  # noqa: E402,F401
    GOALS, PLACEHOLDER, assert_draft_outside_source, body, draft_sessions, drafts, isolated_home, placeholder,
    refresh, run_session, ticks,
)


def _die(case, number):
    """The drafting worker's process ended without finishing its record."""
    dead = subprocess.Popen(["true"])
    dead.wait()
    case.store.update_run(f"draft-{number}", pid=dead.pid, process_identities=[])


def test_a_session_that_died_unfinished_is_drafted_again_up_to_the_cap(drafts):
    from foragent import orch_draft

    case = drafts()
    placeholder(case, 11, "orch")
    ticks(case)
    assert len(draft_sessions(case)) == 1
    for sessions in range(2, orch_draft.MAX_SESSIONS + 1):
        _die(case, 11)
        refresh(case)
        ticks(case)
        assert case.store.get_draft(11).status == "running"
        assert len(draft_sessions(case)) == sessions, ("A dead session leaves the issue eligible again", case.launched)
    _die(case, 11)
    for _ in range(2):
        refresh(case)
        ticks(case)
    assert case.store.get_draft(11).outcome == "error"
    assert len(draft_sessions(case)) == orch_draft.MAX_SESSIONS, "Sessions are capped"
    assert case.calls == []


def test_a_restarted_session_after_a_death_drafts_and_ends_eligibility(drafts):
    case = drafts()
    placeholder(case, 11, "orch")
    ticks(case)
    _die(case, 11)
    refresh(case)
    ticks(case)
    sessions = draft_sessions(case)
    assert len(sessions) == 2
    run_session(case, sessions[-1])
    assert_draft_outside_source(case, 11)
    assert case.store.get_draft(11).outcome == "drafted"
    for _ in range(2):
        refresh(case)
        ticks(case)
    assert draft_sessions(case) == sessions


def test_only_the_exact_placeholder_line_counts():
    from foragent.orch_draft import is_placeholder

    assert is_placeholder(body(GOALS[11], PLACEHOLDER))
    assert not is_placeholder(body(GOALS[11], PLACEHOLDER + "\ntrue"))
    assert not is_placeholder(body(GOALS[11], "false  # placeholder: something else"))
    assert not is_placeholder(body(GOALS[11], "uv run pytest -q tests/acceptance/test_x.py"))


def test_a_draft_does_not_count_toward_its_lanes_plan_width(drafts, monkeypatch):
    from foragent import orch_plan, scheduler_start

    case = drafts()
    placeholder(case, 11, "orch")
    ticks(case)
    monkeypatch.setattr(orch_plan, "width", lambda store, lane, now: 1)
    assert scheduler_start._at_plan_width(case.tick_ctx, "orch") is False


def _agent(case, monkeypatch, *, tests=True, plan=True, exit_code=0, error_kind="normal"):
    """Replace the fixture's lane agent with one that writes only what it is told to."""
    import json

    from foragent import executors
    from foragent.executors import ExecResult

    def agent(self, **kwargs):
        case.calls.append(kwargs)
        tree = Path(kwargs["worktree"])
        if tests:
            test = tree / "tests/acceptance/test_drafted_11.py"
            test.parent.mkdir(parents=True, exist_ok=True)
            test.write_text("def test_drafted():\n    assert False\n")
        if plan:
            (tree / "plan.json").write_text(json.dumps({"id": "draft-11", "issues": []}))
        return ExecResult(exit_code, False, "agent finished", kwargs["log_path"], error_kind=error_kind)

    for executor in (executors.ClaudeExecutor, executors.CodexExecutor):
        monkeypatch.setattr(executor, "run", agent)


def _session(case):
    """Start the issue's next drafting session and run its worker; return the record."""
    from test_skills_autosync import cli_exit

    before = len(draft_sessions(case))
    refresh(case)
    ticks(case)
    sessions = draft_sessions(case)
    assert len(sessions) == before + 1, ("A new drafting session starts", case.launched)
    cli_exit(case.main, list(sessions[-1]))
    return case.store.get_draft(11)


def test_a_finished_draft_stays_finished_after_the_journals_are_trimmed(drafts):
    from datetime import timedelta

    from foragent import orch_draft

    case = drafts()
    placeholder(case, 11, "orch")
    ticks(case)
    run_session(case, draft_sessions(case)[0])
    case.clock[0] += timedelta(days=31)
    assert case.store.trim_journals(case.clock[0]), "The drafting events were old enough to trim"
    assert orch_draft.candidates(case.tick_ctx) == []
    refresh(case)
    ticks(case)
    assert len(draft_sessions(case)) == 1


def test_the_attempt_cap_survives_trimmed_journals(drafts, monkeypatch):
    from datetime import timedelta

    from foragent import orch_draft

    case = drafts()
    placeholder(case, 11, "orch")
    _agent(case, monkeypatch, exit_code=1)
    ticks(case)
    from test_skills_autosync import cli_exit
    cli_exit(case.main, list(draft_sessions(case)[0]))
    for _ in range(orch_draft.MAX_SESSIONS - 1):
        assert _session(case).outcome == "error"
    case.clock[0] += timedelta(days=31)
    case.store.trim_journals(case.clock[0])
    assert orch_draft.candidates(case.tick_ctx) == []


@pytest.mark.parametrize("tests, plan", [(True, False), (False, True)], ids=["tests-only", "plan-only"])
def test_an_incomplete_draft_is_not_kept_and_the_issue_is_drafted_again(drafts, monkeypatch, tests, plan):
    from foragent import orch_draft

    case = drafts()
    placeholder(case, 11, "orch")
    _agent(case, monkeypatch, tests=tests, plan=plan)
    ticks(case)
    from test_skills_autosync import cli_exit
    assert cli_exit(case.main, list(draft_sessions(case)[0])) == 1
    record = case.store.get_draft(11)
    assert (record.outcome, record.attempts) == ("error", 1)
    assert not orch_draft.draft_dir(case.store, 11).exists()
    assert orch_draft.candidates(case.tick_ctx) == [11]
    _agent(case, monkeypatch)
    assert _session(case).outcome == "drafted"
    assert (orch_draft.draft_dir(case.store, 11) / "plan.json").is_file()


def test_a_non_object_plan_is_incomplete(drafts, monkeypatch):
    case = drafts()
    placeholder(case, 11, "orch")
    _agent(case, monkeypatch)
    from foragent import executors

    run = executors.ClaudeExecutor.run

    def agent(self, **kwargs):
        result = run(self, **kwargs)
        (Path(kwargs["worktree"]) / "plan.json").write_text("[]")
        return result

    for executor in (executors.ClaudeExecutor, executors.CodexExecutor):
        monkeypatch.setattr(executor, "run", agent)
    ticks(case)
    from test_skills_autosync import cli_exit
    assert cli_exit(case.main, list(draft_sessions(case)[0])) == 1
    assert case.store.get_draft(11).outcome == "error"


def test_transient_errors_use_no_attempt(drafts, monkeypatch):
    from foragent import orch_draft

    case = drafts()
    placeholder(case, 11, "orch")
    _agent(case, monkeypatch, tests=False, plan=False, exit_code=1, error_kind="transient")
    ticks(case)
    from test_skills_autosync import cli_exit
    cli_exit(case.main, list(draft_sessions(case)[0]))
    for _ in range(orch_draft.MAX_SESSIONS + 1):
        record = _session(case)
        assert (record.outcome, record.attempts) == ("transient", 1)
    _agent(case, monkeypatch)
    assert _session(case).outcome == "drafted"


def test_a_usage_limit_pauses_the_executor_and_uses_no_attempt(drafts, monkeypatch):
    from datetime import timedelta

    from foragent import orch_draft

    case = drafts()
    placeholder(case, 11, "orch")
    _agent(case, monkeypatch, tests=False, plan=False, exit_code=1, error_kind="limit")
    ticks(case)
    from test_skills_autosync import cli_exit
    cli_exit(case.main, list(draft_sessions(case)[0]))
    record = case.store.get_draft(11)
    assert (record.outcome, record.attempts) == ("limit-wait", 1)
    executor = case.ctx.config.executor
    assert case.store.executor_paused(executor, case.clock[0])
    assert orch_draft.candidates(case.tick_ctx) == []
    case.clock[0] = case.store.executor_pause_until(executor, case.clock[0]) + timedelta(seconds=1)
    _agent(case, monkeypatch)
    record = _session(case)
    assert (record.outcome, record.attempts) == ("drafted", 1)


def test_an_issue_recorded_while_candidates_are_read_does_not_break_the_tick(drafts, monkeypatch):
    from foragent import orch_draft

    case = drafts()
    placeholder(case, 11, "orch")
    case.tick_ctx.start_runs = False  # Records the issue rows without starting anything.
    ticks(case)
    read = case.store.local_issues
    calls = []

    def growing():
        rows = read()
        calls.append(len(rows))
        if len(calls) > 1:  # A refresh recorded another issue between two reads.
            rows = [*rows, {**next(row for row in rows if row["number"] == 11), "number": 99}]
        return rows

    monkeypatch.setattr(case.store, "local_issues", growing)
    assert orch_draft.candidates(case.tick_ctx) == [11]
