"""D80 acceptance: supported assignment/goal controls. Locked; do not edit.

Public CLI: orch assign ISSUE --executor NAME --model NAME [--until ISO],
orch assign ISSUE --clear, orch goal TEXT --deadline ISO, orch goal --clear.
Writes take --by and --why; plan --json exposes assignments and goal. The
tests leave storage, helper names and the orchestrator's planning to the lane.
"""

from datetime import timedelta
import json

import pytest

from foragent.scheduler import tick
from test_executor_choice import executor_choice_case, recorded_usage
from test_lane_queue import queue_case  # noqa: F401
from test_local_truth import record_gh
from test_orch_plan import at, fa, fa_ok, plan
from test_orch_review_handoff import bind_cli, tick_context
from test_skills_autosync import isolated_home  # noqa: F401


def choice(case, capsys, number=3):
    shown = plan(case, capsys)
    assert "assignments" in shown, "Plan output must expose saved issue choices"
    return shown["assignments"].get(str(number))


def assert_history(case, *, why, by, at_time, facts):
    journal = case.store.root / "orch-events.jsonl"
    assert journal.is_file(), "A supported change must have durable history"
    rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    matches = [row for row in rows if why in json.dumps(row, ensure_ascii=False)]
    assert len(matches) == 1, (why, rows)
    shown = json.dumps(matches[0], ensure_ascii=False)
    for fact in (why, by, at_time.isoformat(), *facts):
        assert str(fact) in shown, (fact, matches[0])


def test_issue_choice_can_be_inspected_revised_and_undone_with_history(queue_case, capsys):
    case = queue_case
    first_time = case.clock[0]
    fa_ok(case, capsys, "assign", 3, "--executor", "codex", "--model", "offline-first",
          "--by", "orchestrator", "--why", "Use available quota for this issue")
    first = choice(case, capsys)
    assert first["executor"] == "codex" and first["model"] == "offline-first", first
    assert_history(case, why="Use available quota for this issue", by="orchestrator",
                   at_time=first_time, facts=(3, "codex", "offline-first"))
    case.clock[0] += timedelta(minutes=2)
    fa_ok(case, capsys, "assign", 3, "--executor", "claude", "--model", "offline-revised",
          "--by", "human", "--why", "Jin revises this issue's model")
    revised = choice(case, capsys)
    assert revised["executor"] == "claude" and revised["model"] == "offline-revised", revised
    assert_history(case, why="Jin revises this issue's model", by="human",
                   at_time=case.clock[0], facts=(3, "claude", "offline-revised"))
    case.clock[0] += timedelta(minutes=1)
    fa_ok(case, capsys, "assign", 3, "--executor", "codex", "--model", "offline-first",
          "--by", "human", "--why", "Undo the model revision")
    restored = choice(case, capsys)
    assert (restored["executor"], restored["model"]) == ("codex", "offline-first")
    assert_history(case, why="Undo the model revision", by="human",
                   at_time=case.clock[0], facts=(3, "codex", "offline-first"))
    assert case.gh_calls == []


def test_goal_and_deadline_survive_later_sessions_and_can_be_revised_or_cleared(queue_case, capsys):
    case = queue_case
    deadline = at(case, days=2)
    goal = "Deliver the offline planning controls"
    fa_ok(case, capsys, "goal", goal, "--deadline", deadline, "--by", "human",
          "--why", "Jin chooses the delivery goal")
    assert_history(case, why="Jin chooses the delivery goal", by="human",
                   at_time=case.clock[0], facts=(goal, deadline))
    # queue_case creates a fresh StateStore for each CLI call, including these
    # later reads and a plan edit by the orchestrator within Jin's goal.
    case.clock[0] += timedelta(hours=4)
    fa_ok(case, capsys, "queue", "orch", 7, 3, "--by", "orchestrator")
    for _ in range(2):
        current = plan(case, capsys).get("goal")
        assert current and current["text"] == goal and current["deadline"] == deadline, current
    later = at(case, days=3)
    fa_ok(case, capsys, "goal", "Deliver planning and its handover", "--deadline", later,
          "--by", "human", "--why", "Jin expands the delivery")
    current = plan(case, capsys)["goal"]
    assert current["text"] == "Deliver planning and its handover" and current["deadline"] == later
    assert_history(case, why="Jin expands the delivery", by="human", at_time=case.clock[0],
                   facts=("Deliver planning and its handover", later))
    case.clock[0] += timedelta(minutes=1)
    fa_ok(case, capsys, "goal", "--clear", "--by", "human", "--why", "Jin withdraws the goal")
    assert plan(case, capsys).get("goal") is None
    assert_history(case, why="Jin withdraws the goal", by="human", at_time=case.clock[0], facts=())
    assert case.gh_calls == []


@pytest.mark.parametrize("control", ["assignment", "goal"])
def test_orchestrator_cannot_replace_or_clear_a_human_choice(queue_case, capsys, control):
    case = queue_case
    if control == "assignment":
        original = ["assign", 3, "--executor", "codex", "--model", "offline-human"]
        replacement = ["assign", 3, "--executor", "claude", "--model", "offline-proposed"]
        clear = ["assign", 3, "--clear"]
    else:
        original = ["goal", "Jin's goal", "--deadline", at(case, days=2)]
        replacement = ["goal", "A proposed goal", "--deadline", at(case, days=3)]
        clear = ["goal", "--clear"]
    fa_ok(case, capsys, *original, "--by", "human", "--why", "Jin's explicit choice")
    before = plan(case, capsys)
    for change in (replacement, clear):
        code, output = fa(case, capsys, *change, "--by", "orchestrator", "--why", "A proposed revision")
        assert code != 0 and "human" in output.lower(), output
        assert plan(case, capsys) == before, "Refusal must preserve the whole prior plan"
    assert case.gh_calls == []


def test_invalid_assignment_refuses_with_a_reason_and_preserves_the_valid_choice(queue_case, capsys):
    case = queue_case
    fa_ok(case, capsys, "assign", 3, "--executor", "codex", "--model", "offline-valid",
          "--why", "Jin selects a valid executor")
    before = plan(case, capsys)
    code, output = fa(case, capsys, "assign", 3, "--executor", "unavailable-executor",
                      "--model", "offline-invalid", "--why", "Try an unavailable executor")
    assert code != 0 and "unavailable-executor" in output, output
    assert plan(case, capsys) == before
    assert case.gh_calls == []


def test_assignment_never_bypasses_a_real_dependency_or_lane_pause(queue_case, capsys):
    case = queue_case
    fa_ok(case, capsys, "assign", 3, "--executor", "codex", "--model", "offline-safety",
          "--why", "Choose the executor without changing readiness")
    case.tracker.add_issue(11, "Unfinished prerequisite")
    case.tracker.add_blocked_by(3, 11)
    case.tracker.remove_labels(5, ["ready", "approved"])
    case.tracker.remove_labels(7, ["ready", "approved"])
    tick(case.tick_ctx)
    assert case.launched == [] and case.store.get_run(3) is None
    assert choice(case, capsys)["model"] == "offline-safety"
    case.tracker.close_issue(11)
    case.clock[0] += timedelta(minutes=6)
    fa_ok(case, capsys, "pause", "orch", "--until", at(case, hours=1), "--why", "Keep the lane paused")
    tick(case.tick_ctx)
    assert case.launched == [] and case.store.get_run(3) is None
    fa_ok(case, capsys, "resume", "orch")
    report = tick(case.tick_ctx)
    assert case.launched == [3], report.actions
    assert case.gh_calls == []


@pytest.mark.parametrize("release", ["keep", "clear", "expire"])
def test_next_eligible_work_uses_the_choice_and_release_restores_normal_selection(
    git_history, tmp_path, monkeypatch, capsys, release,
):
    from foragent.executors import ExecResult
    from foragent.runner import run_issue
    from foragent.state import StateStore

    case = executor_choice_case(git_history, tmp_path, monkeypatch)
    case.main = bind_cli(case, monkeypatch)
    calls = record_gh(monkeypatch)
    # Usage would ordinarily choose Claude. An explicit Codex assignment wins
    # only while live; expiry and clearing must restore that ordinary choice.
    case.usage = recorded_usage(case.clock[0])
    fa_ok(case, capsys, "assign", 1, "--executor", "codex", "--model", "offline-chosen-model",
          "--until", at(case, minutes=10), "--why", "Use this chosen model for the issue")
    if release == "clear":
        fa_ok(case, capsys, "assign", 1, "--clear", "--why", "Return to ordinary selection")
    elif release == "expire":
        case.clock[0] += timedelta(minutes=11)
        case.usage = recorded_usage(case.clock[0])
    evidence = tmp_path / "executor-invocations.jsonl"

    def execute(name, kwargs):
        # Observe the real runner at its external Executor.run boundary. The
        # fake writes what it received to a file; no private helper is patched.
        with evidence.open("a") as stream:
            stream.write(json.dumps({"executor": name, "model": kwargs.get("model")}) + "\n")
        if len(evidence.read_text().splitlines()) == 1:
            message = case.implement(kwargs)
        else:
            message = json.dumps({"result": "(none)"}) if name == "claude" else "VERDICT: PASS"
        return ExecResult(0, False, message, kwargs["log_path"])

    executors = {"codex": case.implementer, "claude": case.reviewer}
    for name, executor in executors.items():
        monkeypatch.setattr(executor, "run", lambda name=name, **kwargs: execute(name, kwargs))
    case.ctx.executor_factory = executors.__getitem__
    outcomes = []
    context = tick_context(case)
    context.launcher = lambda number: outcomes.append(run_issue(number, case.ctx))
    tick(context)
    assert outcomes == ["merged"], outcomes
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    expected = "codex" if release == "keep" else "claude"
    assert rows[0]["executor"] == expected, rows
    assert (rows[0]["model"] == "offline-chosen-model") == (release == "keep"), rows
    assert len(rows) == 2 and rows[1]["executor"] != expected, "Cross review remains required"
    run = StateStore(case.store.root).get_run(1)
    assert run.executor == expected and run.reviewer != expected
    assert calls == []
