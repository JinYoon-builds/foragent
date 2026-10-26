"""Acceptance test for tick-step-times. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json
import math
from pathlib import Path
import re
import subprocess
import time
from types import SimpleNamespace

import pytest

from foragent import board, cli, i18n, scheduler
from test_orch_review_handoff import bind_cli
from test_scheduler import approve, ctx  # noqa: F401 (fixture)
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_state_split import offline_acceptance  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def local_io_only(tmp_path, monkeypatch, offline_acceptance):
    """Only local fixture Git may run; HOME/network/signals are guarded by shared fixtures."""
    popen = subprocess.Popen

    def local_git(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv[0] == "git", argv
        assert Path(kwargs["cwd"]).resolve().is_relative_to(tmp_path.resolve()), kwargs
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_git)
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    # Git's subprocess timeout polling may sleep internally; keep even that
    # independent of wall time. Collaborator probes advance the fake clock.
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


@pytest.fixture
def timed_tick(ctx, monkeypatch):
    remote = ctx.tracker._tracker
    project = board.ensure(remote, ctx.repo)
    ctx.config = replace(ctx.config, repo=ctx.repo, board=project, skill_libraries=[],
                         notify="none", test_command="true")
    ctx.start_runs = True
    approve(ctx, 1, labels=["ready"])
    board.add_issue(remote, ctx.repo, project, 1)
    case = SimpleNamespace(
        ctx=ctx, root=ctx.repo_root, git=ctx.git, tracker=remote, store=ctx.store,
        clock=[ctx.now()], elapsed=1000.0, starts=[], events=[], failure=None,
        # Distinct fractional durations identify the required steps without
        # prescribing their names or a particular timing helper API.
        delays={"github": 0.25, "recovery": 0.5, "board": 0.75, "start": 1.0, "view": 1.25},
    )
    ctx.now = lambda: case.clock[0]
    ctx.launcher = lambda number: case.starts.append(number)  # None PID; no real worker.
    case.main = bind_cli(case, monkeypatch)
    # Keep the real CLI outcome wrapper and scheduler; inject only the shared
    # offline context, whose local Git and FakeTracker need no account/config fetch.
    monkeypatch.setattr(cli, "TickContext", lambda **kwargs: ctx)
    monkeypatch.setattr(cli, "spawn_runner", lambda *args: ctx.launcher)
    monkeypatch.setattr(cli, "spawn_notify", lambda *args: None)
    monkeypatch.setattr(time, "monotonic", lambda: case.elapsed)
    monkeypatch.setattr(time, "perf_counter", lambda: case.elapsed)

    def unexpected(*args, **kwargs):
        pytest.fail("Timing a tick must not launch a baseline process")

    ctx.baseline_launcher = unexpected
    monkeypatch.setattr(cli, "start_baseline_process", unexpected)

    def instrument(owner, attribute, name, *, ready_board_only=False):
        original = getattr(owner, attribute)

        def measured(*args, **kwargs):
            # Starting a run also updates its board card. Give only the earlier
            # Ready sync a duration, keeping these five probes nonoverlapping.
            if ready_board_only and args[1] != "Ready":
                return original(*args, **kwargs)
            case.events.append(name)
            try:
                if name == "recovery" and case.failure is not None:
                    raise case.failure
                return original(*args, **kwargs)
            finally:
                case.elapsed += case.delays[name]

        monkeypatch.setattr(owner, attribute, measured)

    instrument(remote, "list_issues", "github")
    instrument(scheduler, "_recover_runs", "recovery")
    instrument(board, "set_status", "board", ready_board_only=True)
    instrument(ctx, "launcher", "start")
    instrument(scheduler.watch_open, "reopen", "view")
    return case


def _tick(case, capsys):
    # Reuse the same ready work on each tick, without a live PID to clean up.
    if case.store.get_run(1) is not None:
        case.store.finish_run(1, "cancelled")
        case.tracker.remove_labels(1, ["running"])
        case.tracker.add_labels(1, ["ready"])
    case.clock[0] += timedelta(minutes=5)
    case.events.clear()
    launches = len(case.starts)
    capsys.readouterr()
    try:
        code = cli_exit(case.main, ["gate", "tick", "--json", "--repo-root", str(case.root)])
    except RuntimeError as exc:
        assert exc is case.failure, "Only the injected scheduler failure may escape"
        code = 1
    output = capsys.readouterr()
    assert not case.ctx.process_table.signals
    if case.failure is None:
        assert code == 0, output.err
        assert json.loads(output.out)["skipped"] is None
        assert case.starts[launches:] == [1], "Measuring time must still start ready work once"
        assert case.store.get_run(1).status == "running"
        assert "running" in case.tracker.get_issue(1).labels
        assert "ready" not in case.tracker.get_issue(1).labels
        assert case.ctx.tracker.board_value(1, case.ctx.config.board, "Status") == board.board_column("Running")
    else:
        assert code not in (None, 0), "A failed step must still fail the tick"
        assert case.starts[launches:] == []
    path = case.store.root / "tick-outcome.json"
    assert path.is_file(), "Both successful and failing gate ticks must persist their outcome"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["status"] == ("failed" if case.failure is not None else "ok")
    return SimpleNamespace(record=record, stderr=output.err, events=list(case.events))


def _steps(record):
    assert "steps" in record, "tick-outcome.json must record elapsed time for each top-level step"
    steps = record["steps"]
    assert isinstance(steps, list) and steps, "steps must be a nonempty ordered list"
    for step in steps:
        assert isinstance(step, dict) and {"name", "seconds"} <= step.keys(), step
        assert isinstance(step["name"], str) and step["name"].strip(), step
        assert type(step["seconds"]) in (int, float) and math.isfinite(step["seconds"]), step
        assert step["seconds"] >= 0, step
    duration = record["duration_seconds"]
    assert type(duration) in (int, float) and math.isfinite(duration) and duration >= 0
    assert sum(step["seconds"] for step in steps) <= duration, "Step timings must not double-count elapsed time"
    return steps


def _marked_steps(result, delays):
    steps = _steps(result.record)
    positions, names = [], {}
    for event in result.events:
        matches = [index for index, step in enumerate(steps)
                   if step["seconds"] == pytest.approx(delays[event])]
        assert len(matches) == 1, f"Record {event}'s {delays[event]} monotonic seconds exactly once: {steps}"
        positions.append(matches[0])
        names[event] = steps[matches[0]]["name"]
    assert positions == sorted(positions), "Record steps in their actual execution order"
    assert len(set(names.values())) == len(names), "The required steps need distinct, stable names"
    return names


def _slow_line(result, seconds):
    lines = result.stderr.splitlines()
    assert len(lines) == 1, f"One slow step needs exactly one stderr diagnostic, got {result.stderr!r}"
    numbers = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", lines[0])]
    assert any(value == pytest.approx(seconds) for value in numbers), lines[0]
    return lines[0]


def test_tick_step_times_normal_tick_records_ordered_monotonic_steps_with_stable_names(timed_tick, capsys):
    case = timed_tick
    first, second = _tick(case, capsys), _tick(case, capsys)
    assert first.events == second.events and set(first.events) == set(case.delays)
    assert len(first.events) == len(case.delays), "Each required phase must run once"
    first_names = _marked_steps(first, case.delays)
    assert _marked_steps(second, case.delays) == first_names
    # The wall clock is frozen inside each tick. Only the monotonic clock moves.
    for result in (first, second):
        assert result.record["duration_seconds"] == pytest.approx(sum(case.delays.values()))
        assert result.stderr == ""


def test_tick_step_times_75_seconds_warns_through_messages_without_changing_work(timed_tick, monkeypatch, capsys):
    case = timed_tick
    monkeypatch.setitem(i18n.MESSAGES, "en", {
        key: "FROM-MESSAGES: " + template for key, template in i18n.MESSAGES["en"].items()
    })
    normal_delays = dict(case.delays)
    normal = _tick(case, capsys)
    case.delays["github"] = 75.0
    slow = _tick(case, capsys)
    assert slow.events == normal.events, "Elapsed time must not skip, retry or reorder work"
    line = _slow_line(slow, 75.0)
    assert "FROM-MESSAGES:" in line, "Slow-step diagnostics must use the messages registry"
    names = _marked_steps(slow, case.delays)
    assert names["github"] in line, "Name the same step that recorded the 75 seconds"
    assert _marked_steps(normal, normal_delays) == names
    assert normal.stderr == ""


def test_tick_step_times_warning_threshold_is_inclusive_and_applies_to_each_step(timed_tick, capsys):
    case = timed_tick
    case.delays["github"] = 59.75
    below_delays = dict(case.delays)
    below = _tick(case, capsys)
    assert below.record["duration_seconds"] > 60, "The whole tick exceeds 60s even though no step does"
    assert below.stderr == "", "A step below 60 seconds must not warn"
    case.delays["github"] = 60.0
    boundary = _tick(case, capsys)
    assert boundary.events == below.events
    line = _slow_line(boundary, 60.0)
    names = _marked_steps(boundary, case.delays)
    assert names["github"] in line
    assert _marked_steps(below, below_delays) == names


def test_tick_step_times_failure_keeps_completed_and_failing_steps_without_stale_tail(timed_tick, capsys):
    case = timed_tick
    normal_delays = dict(case.delays)
    normal = _tick(case, capsys)
    case.failure = RuntimeError("offline recovery step failed")
    case.delays["recovery"] = 7.25
    failed = _tick(case, capsys)
    assert failed.events == ["github", "recovery"], "The injected recovery failure must be reached"
    assert failed.record["error_type"] == "RuntimeError"
    assert failed.record["error_message"] == str(case.failure)
    failed_names = _marked_steps(failed, case.delays)
    normal_names = _marked_steps(normal, normal_delays)
    assert failed_names == {event: normal_names[event] for event in failed.events}
    before, after = _steps(normal.record), _steps(failed.record)
    stop = next(index for index, step in enumerate(before) if step["name"] == normal_names["recovery"])
    failed_stop = next(index for index, step in enumerate(after) if step["name"] == failed_names["recovery"])
    assert [step["name"] for step in after[:failed_stop + 1]] == [step["name"] for step in before[:stop + 1]]
    assert {normal_names[event] for event in ("board", "start", "view")}.isdisjoint(
        step["name"] for step in after
    ), "A failure must not retain later steps from the previous tick"
    assert failed.record["duration_seconds"] == pytest.approx(0.25 + 7.25)
