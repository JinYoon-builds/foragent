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


def wide_lane(case, monkeypatch, files):
    """Give each waiting issue declared Files and free machine budget, as the lane-clone test does."""
    from dataclasses import replace

    from foragent import scheduler
    from foragent.capacity import write_state

    case.tick_ctx.config = replace(case.tick_ctx.config, max_parallel=4)
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.0))
    write_state(case.store, 4, case.clock[0] - timedelta(hours=1))
    body = case.tracker.get_issue(3).body
    for number, paths in files.items():
        listed = body + "\n## Files\n" + "".join(f"- {path}\n" for path in paths)
        case.tracker.add_issue(number, f"Task {number}", listed, labels=["ready", "lane:orch"])
    # Approve after the body edit, so each approval covers the declared Files.
    case.clock[0] += timedelta(seconds=1)
    for number in files:
        case.tracker.record_label(number, "approved", "human")


def tick_lane(case, ticks=4):
    reports = [tick(case.tick_ctx) for _ in range(ticks)]
    for report in reports:
        assert report.skipped is None, report
    actions = [action for report in reports for action in report.actions]
    running = sorted(run.number for run in case.store.active_runs() if run.lane == "orch")
    return running, actions


def test_plan_width_lets_a_lane_run_three_disjoint_issues(queue_case, capsys, monkeypatch):
    case = queue_case
    wide_lane(case, monkeypatch, {3: ["src/foragent/state.py"], 5: ["src/foragent/order.py"],
                                  7: ["src/foragent/watch.py"]})
    fa_ok(case, capsys, "plan", "--width", "orch=3")
    assert plan(case, capsys)["width"]["orch"] == 3
    stored = json.loads((case.store.root / "plan.json").read_text(encoding="utf-8"))
    assert stored["width"]["orch"] == 3, stored
    [event] = plan_events(case, "plan-width")
    text = json.dumps(event, ensure_ascii=False)
    for fact in ("orch", "3", "human"):
        assert fact in text, (fact, event)

    running, actions = tick_lane(case)
    assert running == [3, 5, 7], actions
    assert sorted(case.launched) == [3, 5, 7], actions
    assert case.gh_calls == []


def test_lane_width_is_only_a_cap_set_by_a_live_plan(queue_case, capsys, monkeypatch):
    """Planning addendum r10 (Jin): no fixed per-lane cap; fill free slots with any ready
    issue whose Files do not overlap a running one. A plan width is an optional cap."""
    case = queue_case
    wide_lane(case, monkeypatch, {3: ["src/foragent/state.py"], 5: ["src/foragent/order.py"],
                                  7: ["src/foragent/watch.py"]})
    assert plan(case, capsys).get("width", {}) == {}
    # The clock jumps an hour below; keep the stall rule out of this width check.
    from dataclasses import replace
    case.tick_ctx.config = replace(case.tick_ctx.config, stall_minutes=120)
    fa_ok(case, capsys, "plan", "--width", "orch=2", "--expires", at(case, hours=1))
    assert plan(case, capsys)["width"]["orch"] == 2
    running, actions = tick_lane(case)
    assert len(running) == 2, actions
    # Once the plan expires its width lapses; with no fixed cap all disjoint issues run.
    case.clock[0] += timedelta(hours=1, seconds=1)
    running, actions = tick_lane(case)
    assert running == [3, 5, 7], actions
    assert sorted(case.launched) == running, actions
    assert case.gh_calls == []


def test_plan_width_never_starts_overlapping_files(queue_case, capsys, monkeypatch):
    case = queue_case
    # 7 shares a file with 5, not with the first run 3: every running issue counts.
    wide_lane(case, monkeypatch, {3: ["src/foragent/state.py"], 5: ["src/foragent/order.py"],
                                  7: ["src/foragent/order.py"], 9: ["src/foragent/watch.py"]})
    # Pin the overlap cap to one run per file, so the cap (not the width) holds 7 back.
    from dataclasses import replace
    case.tick_ctx.config = replace(case.tick_ctx.config, overlap_per_file=1)
    fa_ok(case, capsys, "plan", "--width", "orch=3")
    running, actions = tick_lane(case, ticks=5)
    assert running == [3, 5, 9], actions
    assert 7 not in case.launched, actions
    assert case.store.get_run(7) is None
    assert "ready" in case.tracker.get_issue(7).labels
    assert "running" not in case.tracker.get_issue(7).labels
    assert case.gh_calls == []
