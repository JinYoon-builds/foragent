"""Acceptance test for orch-plan. Locked by Foragent; do not edit."""

from datetime import datetime, timedelta
import json

from foragent.scheduler import tick
from test_lane_queue import assert_tick_starts, finish_issue, queue_case, queue_output  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401

WHY = "Hold orch while the schema migration lands"


def fa(case, capsys, *arguments):
    argv = ["orch", *map(str, arguments)]
    code = cli_exit(case.main, argv)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def fa_ok(case, capsys, *arguments):
    code, output = fa(case, capsys, *arguments)
    assert code == 0, f"fa orch {' '.join(map(str, arguments))} must succeed (exit={code}):\n{output}"
    return output


def plan(case, capsys):
    argv = ["orch", "plan", "--json"]
    code = cli_exit(case.main, argv)
    captured = capsys.readouterr()
    assert code == 0, f"fa orch plan --json must succeed (exit={code}):\n{captured.err}"
    return json.loads(captured.out)


def plan_events(case, kind):
    path = case.store.root / "orch-events.jsonl"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [event for event in events if event.get("kind") == kind]


def at(case, **delta):
    return (case.clock[0] + timedelta(**delta)).isoformat()


def assert_tick_starts_nothing(case):
    report = tick(case.tick_ctx)
    assert case.launched == [], report.actions
    assert case.store.active_runs() == [], report.actions


def test_pause_needs_a_reason_and_holds_the_lane_until_resume(queue_case, capsys):
    case = queue_case
    until = at(case, hours=8)
    code, output = fa(case, capsys, "pause", "orch", "--until", until)
    assert code != 0, output
    assert "--why" in output, output
    assert plan_events(case, "plan-pause") == []

    fa_ok(case, capsys, "pause", "orch", "--until", until, "--why", WHY)
    paused = plan(case, capsys)["paused"]["orch"]
    assert paused["why"] == WHY, paused
    assert datetime.fromisoformat(paused["until"]) == datetime.fromisoformat(until), paused
    [event] = plan_events(case, "plan-pause")
    text = json.dumps(event, ensure_ascii=False)
    for fact in ("orch", WHY, "human", until[:16]):
        assert fact in text, (fact, event)
    assert_tick_starts_nothing(case)
    assert_tick_starts_nothing(case)

    fa_ok(case, capsys, "resume", "orch")
    assert "orch" not in plan(case, capsys)["paused"]
    [event] = plan_events(case, "plan-resume")
    text = json.dumps(event, ensure_ascii=False)
    assert "orch" in text and "human" in text, event
    assert_tick_starts(case, [3])
    assert case.gh_calls == []


def test_pause_lapses_by_itself_once_until_passes(queue_case, capsys):
    case = queue_case
    fa_ok(case, capsys, "pause", "orch", "--until", at(case, hours=2), "--why", WHY)
    assert_tick_starts_nothing(case)
    case.clock[0] += timedelta(hours=1, minutes=59)
    assert_tick_starts_nothing(case)
    case.clock[0] += timedelta(minutes=1, seconds=1)
    assert_tick_starts(case, [3])
    assert plan_events(case, "plan-resume") == []  # Lapsing is not a tool call.
    assert case.gh_calls == []


def test_plan_file_holds_queues_pauses_expiry_and_author(queue_case, capsys):
    case = queue_case
    queue_output(case, capsys, "orch", 7, 3)
    fa_ok(case, capsys, "pause", "orch", "--until", at(case, hours=8), "--why", WHY)
    expires = at(case, hours=12)
    fa_ok(case, capsys, "plan", "--expires", expires)

    shown = plan(case, capsys)
    assert shown["queues"]["orch"] == [7, 3], shown
    assert shown["paused"]["orch"]["why"] == WHY, shown
    assert datetime.fromisoformat(shown["expires_at"]) == datetime.fromisoformat(expires), shown
    assert shown["written_by"] == "human", shown
    # One per-repository file in the state folder is the single source of truth.
    stored = json.loads((case.store.root / "plan.json").read_text(encoding="utf-8"))
    assert stored["queues"]["orch"] == [7, 3], stored
    assert stored["paused"]["orch"]["why"] == WHY, stored
    assert datetime.fromisoformat(stored["expires_at"]) == datetime.fromisoformat(expires), stored
    assert case.gh_calls == []


def test_expired_plan_falls_back_to_defaults_with_one_event(queue_case, capsys):
    case = queue_case
    queue_output(case, capsys, "orch", 7, 5)
    fa_ok(case, capsys, "pause", "orch", "--until", at(case, hours=8), "--why", WHY)
    fa_ok(case, capsys, "plan", "--expires", at(case, hours=1))
    assert_tick_starts_nothing(case)
    assert plan_events(case, "plan-expired") == []

    # The pause would still hold for hours; only expiry releases it.
    case.clock[0] += timedelta(hours=1, seconds=1)
    assert_tick_starts(case, [3])
    assert_tick_starts(case, [3])
    finish_issue(case, 3)
    assert_tick_starts(case, [3, 5])
    tick(case.tick_ctx)
    assert len(plan_events(case, "plan-expired")) == 1
    assert case.gh_calls == []


def test_safety_lines_win_over_the_plan(queue_case, capsys):
    case = queue_case
    case.tracker.add_issue(11, "Real prerequisite")
    case.tracker.add_blocked_by(7, 11)
    queue_output(case, capsys, "orch", 7, 3)
    fa_ok(case, capsys, "pause", "orch", "--until", at(case, hours=8), "--why", WHY)
    # A queue that names an issue first never unpauses its lane.
    assert_tick_starts_nothing(case)
    assert_tick_starts_nothing(case)

    fa_ok(case, capsys, "resume", "orch")
    assert_tick_starts(case, [3])
    assert case.store.get_run(7) is None
    assert case.gh_calls == []


def test_orchestrator_cannot_overwrite_what_a_person_wrote(queue_case, capsys):
    case = queue_case
    fa_ok(case, capsys, "queue", "orch", 5, 7, "--by", "orchestrator")
    fa_ok(case, capsys, "queue", "orch", 7, 5, "--by", "orchestrator")
    queue_output(case, capsys, "orch", 7, 3)

    code, output = fa(case, capsys, "queue", "orch", 3, 7, "--by", "orchestrator")
    assert code != 0, output
    assert "human" in output.lower(), output
    assert plan(case, capsys)["queues"]["orch"] == [7, 3]

    fa_ok(case, capsys, "pause", "orch", "--until", at(case, hours=8), "--why", WHY, "--by", "human")
    code, output = fa(case, capsys, "resume", "orch", "--by", "orchestrator")
    assert code != 0, output
    assert "human" in output.lower(), output
    assert "orch" in plan(case, capsys)["paused"]
    assert plan_events(case, "plan-resume") == []
    assert_tick_starts_nothing(case)
    assert case.gh_calls == []
