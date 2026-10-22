"""Acceptance test for executor-choice. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from test_local_truth import record_gh
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home


@pytest.fixture(autouse=True)
def executor_choice_offline(monkeypatch):
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    calls = record_gh(monkeypatch)
    yield
    assert calls == [], "Executor choice must use recorded usage without gh calls"


def recorded_usage(now, *, codex=(("7d", 67, 72),), claude=(("five_hour", 5, 4),)):
    """D60's public usage shape; reset horizons are hours from the supplied clock."""
    return {
        executor: {
            "seen_at": now.isoformat(),
            "windows": [
                {"name": name, "used_percent": used,
                 "resets_at": (now + timedelta(hours=hours)).isoformat()}
                for name, used, hours in windows
            ],
        }
        for executor, windows in (("codex", codex), ("claude", claude))
    }


def executor_choice_case(git_history, tmp_path, monkeypatch, **config_values):
    case = make_case(
        git_history, tmp_path, monkeypatch,
        **{"executor": "codex", "reviewer": None, "skill_libraries": [],
           "test_command": "true", "lanes": {"orch": {"paths": ["**"]}}, **config_values},
    )
    # make_case normally prepares an already running issue. Let the real gate
    # create its first run here, with a human approval event and no past attempt.
    (case.store.root / "runs/1.json").unlink()
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["ready", "lane:orch"])
    case.tracker.record_label(1, "approved", "human")
    case.usage = recorded_usage(case.clock[0])
    # Usage collection is covered by usage-read. Inject only the public read
    # boundary so these tests exercise D74 even before D60's reader exists.
    monkeypatch.setattr(case.store, "usage", lambda: case.usage, raising=False)
    return case


def fake_executor_calls(case, monkeypatch, implement=None):
    """Reuse the runner fixture's fake executors for either implementation role."""
    from foragent.executors import ExecResult

    calls = []
    executors = {"codex": case.implementer, "claude": case.reviewer}

    def execute(name, kwargs):
        run = case.store.get_run(1)
        phase = run.phase
        calls.append({"executor": name, "phase": phase, "prompt": kwargs["prompt"],
                      "pair": (getattr(run, "executor", None), getattr(run, "reviewer", None))})
        if phase == "reviewing":
            message = json.dumps({"result": "(none)"}) if name == "claude" else "VERDICT: PASS"
        else:
            assert phase == "implementing", phase
            if implement is not None:
                return implement(name, kwargs)
            message = case.implement(kwargs)
        return ExecResult(0, False, message, kwargs["log_path"])

    for name, executor in executors.items():
        monkeypatch.setattr(executor, "run", lambda name=name, **kw: execute(name, kw))
    case.ctx.executor_factory = executors.__getitem__
    return calls


def start_through_gate(case):
    """Keep tick/run_issue real; only replace the process launch with an inline call."""
    from foragent.runner import run_issue
    from foragent.scheduler import tick

    launches, outcomes = [], []
    context = tick_context(case)

    def launch(number):
        run = case.store.get_run(number)
        launches.append((getattr(run, "executor", None), getattr(run, "reviewer", None)))
        outcomes.append(run_issue(number, case.ctx))
        return None

    context.launcher = launch
    report = tick(context)
    assert report.skipped is None, report
    return context, launches, outcomes


def test_executor_choice_headroom_example_bottleneck_windows_and_keep_share(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import choose_executor
    from foragent.state import StateStore

    case = executor_choice_case(git_history, tmp_path, monkeypatch)
    now = case.clock[0]
    # 33/72 < (80-5)/4: Claude implements even though Codex is the default.
    assert choose_executor(case.store, case.config, "orch", now) == ("claude", "codex")
    calls = fake_executor_calls(case, monkeypatch)
    _, launches, outcomes = start_through_gate(case)
    assert outcomes == ["merged"]
    assert launches == [("claude", "codex")], "Persist the pair before launching the runner"
    assert [(call["executor"], call["phase"]) for call in calls] == [
        ("claude", "implementing"), ("codex", "reviewing"),
    ]
    run = StateStore(case.store.root).get_run(1)
    assert (run.executor, run.reviewer) == ("claude", "codex")

    scenarios = [
        # More remaining percent alone is insufficient: 80/72 < 30/4.
        (recorded_usage(now, codex=(("7d", 20, 72),), claude=(("five_hour", 50, 4),)), "claude"),
        # Claude's weekly window constrains it: min(10/1, 75/150) < 50/72.
        (recorded_usage(now, codex=(("7d", 50, 72),),
                        claude=(("five_hour", 70, 1), ("seven_day", 5, 150))), "codex"),
        # Codex's shorter window constrains it: min(100/1, 0.1/4) < 1/4.
        (recorded_usage(now, codex=(("7d", 0, 1), ("5h", 99.9, 4)),
                        claude=(("five_hour", 79, 4),)), "claude"),
        # Keep-share changes the winner: 40/2 > 75/4, but 40/2 < 95/4.
        (recorded_usage(now, codex=(("7d", 60, 2),)), "codex"),
        # Usage past Claude's keep cap leaves zero headroom.
        (recorded_usage(now, claude=(("five_hour", 85, 4),)), "codex"),
    ]
    for usage, expected in scenarios:
        case.usage = usage
        other = "claude" if expected == "codex" else "codex"
        assert choose_executor(case.store, case.config, "orch", now) == (expected, other), usage


def test_executor_choice_fixed_yaml_lane_and_configured_reviewer_win(
    git_history, tmp_path, monkeypatch,
):
    from foragent.config import load_config
    from foragent.scheduler import choose_executor
    from foragent.state import StateStore

    case = executor_choice_case(git_history, tmp_path, monkeypatch)
    path = case.root / ".foragent.yml"
    path.write_text("executor: claude\nreviewer: null\nlanes:\n  orch:\n    executor: codex\n")
    fixed = load_config(case.root)
    assert choose_executor(case.store, fixed, "orch", case.clock[0]) == ("codex", "claude")
    case.ctx.config = replace(case.config, executor=fixed.executor, lanes=fixed.lanes)
    calls = fake_executor_calls(case, monkeypatch)
    _, launches, outcomes = start_through_gate(case)
    assert outcomes == ["merged"] and launches == [("codex", "claude")]
    assert [call["executor"] for call in calls] == ["codex", "claude"]
    run = StateStore(case.store.root).get_run(1)
    assert (run.executor, run.reviewer) == ("codex", "claude")
    # A configured reviewer is honored even when it is the same executor.
    path.write_text("executor: codex\nreviewer: claude\nlanes:\n  orch:\n    executor: claude\n")
    case.usage = recorded_usage(case.clock[0], codex=(("7d", 0, 1),))
    assert choose_executor(case.store, load_config(case.root), "orch", case.clock[0]) == ("claude", "claude")


def test_executor_choice_retry_keeps_recorded_executor_after_headroom_flips(
    git_history, tmp_path, monkeypatch,
):
    from foragent.executors import ExecResult
    from foragent.state import StateStore

    case = executor_choice_case(git_history, tmp_path, monkeypatch)
    attempts = []

    def implement(name, kwargs):
        attempts.append(name)
        if len(attempts) == 1:
            # An ordinary failed Done when check is a retry, not a quota handover.
            case.usage = recorded_usage(case.clock[0], codex=(("7d", 0, 1),),
                                        claude=(("five_hour", 79, 4),))
            message = "Incomplete: feature.txt still needs to be created"
        else:
            message = case.implement(kwargs)
        return ExecResult(0, False, message, kwargs["log_path"])

    calls = fake_executor_calls(case, monkeypatch, implement)
    _, launches, outcomes = start_through_gate(case)
    assert outcomes == ["merged"]
    assert attempts == ["claude", "claude"], "A headroom change must not reassign an in-progress issue"
    assert launches == [("claude", "codex")]
    assert all(call["pair"] == ("claude", "codex") for call in calls)
    assert [call["executor"] for call in calls if call["phase"] == "reviewing"] == ["codex"]
    run = StateStore(case.store.root).get_run(1)
    assert run.attempts == 2 and (run.executor, run.reviewer) == ("claude", "codex")
    from foragent.scheduler import choose_executor

    assert choose_executor(case.store, case.config, "orch", case.clock[0]) == ("codex", "claude")


def test_executor_choice_limit_hands_next_attempt_to_other_executor_with_summary(
    git_history, tmp_path, monkeypatch,
):
    from foragent.executors import ExecResult
    from foragent.scheduler import tick
    from foragent.state import StateStore

    case = executor_choice_case(git_history, tmp_path, monkeypatch)
    case.usage = recorded_usage(case.clock[0], codex=(("7d", 0, 1),))
    summary = "Created the parser skeleton in feature.txt; remaining work: validate empty input."
    attempts = []

    def implement(name, kwargs):
        attempts.append(name)
        if len(attempts) == 1:
            (kwargs["worktree"] / "feature.txt").write_text("parser skeleton\n")
            with kwargs["log_path"].open("a") as log:
                log.write(summary + "\n")
            case.usage = recorded_usage(case.clock[0], codex=(("7d", 100, 72),))
            return ExecResult(1, False, summary, kwargs["log_path"], error_kind="limit")
        return ExecResult(0, False, case.implement(kwargs), kwargs["log_path"])

    calls = fake_executor_calls(case, monkeypatch, implement)
    context, launches, outcomes = start_through_gate(case)
    # Accept an immediate handover or one on the next eligible gate tick.
    # Advancing the fake clock also accommodates the existing limit pause.
    case.clock[0] += timedelta(minutes=case.config.limit_wait_minutes + 1)
    tick(context)
    assert outcomes[-1] == "merged", outcomes
    assert attempts == ["codex", "claude"], "Only a quota limit permits an executor switch"
    implementation = [call for call in calls if call["phase"] == "implementing"]
    assert "Previous attempt" not in implementation[0]["prompt"]
    prompt = implementation[1]["prompt"]
    assert "Previous attempt" in prompt, "The replacement executor needs a handover summary section"
    previous = prompt.partition("Previous attempt")[2]
    assert "parser skeleton" in previous and "empty input" in previous, prompt
    assert launches[0] == ("codex", "claude")
    assert [call["executor"] for call in calls if call["phase"] == "reviewing"] == ["codex"]
    run = StateStore(case.store.root).get_run(1)
    assert (run.executor, run.reviewer) == ("claude", "codex")


def test_executor_choice_no_recorded_usage_uses_configured_default(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import choose_executor
    from foragent.state import StateStore

    case = executor_choice_case(git_history, tmp_path, monkeypatch, executor="claude")
    case.usage = {}
    assert choose_executor(case.store, case.config, "orch", case.clock[0]) == ("claude", "codex")
    assert choose_executor(case.store, replace(case.config, executor="codex"),
                           "orch", case.clock[0]) == ("codex", "claude")
    calls = fake_executor_calls(case, monkeypatch)
    _, launches, outcomes = start_through_gate(case)
    assert outcomes == ["merged"] and launches == [("claude", "codex")]
    assert [call["executor"] for call in calls] == ["claude", "codex"]
    run = StateStore(case.store.root).get_run(1)
    assert (run.executor, run.reviewer) == ("claude", "codex")
