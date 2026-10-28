"""Boundaries of the ordered executors list (#786) beyond the acceptance test."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json

import pytest
import yaml

from foragent.config import Config, ConfigError, load_config
from foragent.orch_inbox import questions
from foragent.scheduler_quota import (
    Switch, credits, decide, record_cost, switch_journal, switch_line, switched_config,
)
from foragent.state import StateStore

from test_scheduler import approve as sched_approve, ctx  # noqa: F401

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def load(tmp_path, data, global_data=None):
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump(data))
    global_path = tmp_path / "global.yml"
    if global_data is not None:
        global_path.write_text(yaml.safe_dump(global_data))
    return load_config(tmp_path, global_path=global_path)


def entry(name, quota="subscription", **options):
    return {"name": name, "quota": quota, **options}


def store_at(tmp_path):
    return StateStore(tmp_path / "state", now=lambda: NOW)


@pytest.mark.parametrize("data,key", [
    ({"executors": []}, "executors"),
    ({"executors": None}, "executors"),
    ({"executors": [entry("local", "unlimited", keep_percent=None)]}, "keep_percent"),
    ({"executors": [entry("local", "unlimited", spend_cap=None)]}, "spend_cap"),
    ({"executors": [entry("claude", keep_percent=None)]}, "keep_percent"),
    ({"executors": [entry("claude", "api", spend_cap=None)]}, "spend_cap"),
    ({"executors": [entry("claude"), entry("claude")]}, "twice"),
    ({"executors": [entry("local", "unlimited", keep_percent=50)]}, "keep_percent"),
    ({"executors": [entry("local", "unlimited", spend_cap=5)]}, "spend_cap"),
    ({"executors": [entry("claude", "api", spend_cap=5, keep_percent=50)]}, "keep_percent"),
    ({"executors": [entry("claude", keep_percent=0)]}, "keep_percent"),
    ({"executors": [entry("claude", spend_cap=0)]}, "spend_cap"),
    ({"executors": [entry("claude")], "claude_quota": {"mode": "burn"}}, "not both"),
    ({"executors": [entry("claude")], "executor": "codex"}, "first executors entry"),
])
def test_invalid_executor_lists_are_config_errors(tmp_path, data, key):
    with pytest.raises(ConfigError, match=key):
        load(tmp_path, data)


def test_global_executors_is_an_error_and_repo_list_wins_over_global_claude_quota(tmp_path):
    with pytest.raises(ConfigError, match="only in the repository"):
        load(tmp_path, {}, {"executors": [entry("claude")]})
    config = load(tmp_path, {"executors": [entry("claude"), entry("codex")], "executor": "claude"},
                  {"claude_quota": {"mode": "keep", "keep_percent": 10}})
    assert config.executor == "claude" and config.executors[0].get("keep_percent") is None
    store = store_at(tmp_path)
    store.record_usage("claude", [{"name": "five_hour", "used_percent": 50, "resets_at": None}])
    assert decide(store, config, "main", NOW).config.executor == "claude", "global keep_percent 10 is ignored"


def test_legacy_lists_are_derived_from_the_executor():
    assert [e["name"] for e in Config(executor="claude").executors] == ["claude", "codex"]
    assert Config(executor="claude").executors[0]["keep_percent"] == 80
    assert [e["name"] for e in Config(executor="codex").executors] == ["codex"]
    assert [e["name"] for e in replace(Config(executor="claude"), executor="codex").executors] == ["codex"]


def test_lane_executor_wraps_around_and_unlisted_lane_executor_stands_alone(tmp_path):
    config = load(tmp_path, {"reviewer": "none", "executors": [entry("claude"), entry("codex"), entry("local")],
                             "lanes": {"ui": {"executor": "codex"}, "solo": {"executor": "claude"}}})
    store = store_at(tmp_path)
    store.pause_executor("codex", NOW + timedelta(hours=1))
    assert decide(store, config, "ui", NOW).config.executor == "local"
    store.pause_executor("local", NOW + timedelta(hours=1))
    start = decide(store, config, "ui", NOW)
    assert (start.config.executor, start.switch.source, start.switch.target) == ("claude", "codex", "claude")
    solo = load(tmp_path, {"reviewer": "none", "executors": [entry("codex")],
                           "lanes": {"solo": {"executor": "claude"}}})
    store.pause_executor("claude", NOW + timedelta(hours=1))
    assert decide(store, solo, "solo", NOW).held, "a lane executor outside the list has no fallback"


def test_switch_event_and_line_use_the_real_names(tmp_path):
    store = store_at(tmp_path)
    switch = Switch("Codex paused at its limit until x", "switch_reason_limit",
                    {"name": "Codex", "until": "x"}, "codex", "local")
    store.start_run(1, branch="b", worktree="w", implementer="local", journal=switch_journal(store, NOW, 1, switch))
    [event] = [json.loads(line) for line in (store.root / "orch-events.jsonl").read_text().splitlines()]
    assert (event["kind"], event["from"], event["to"]) == ("executor-switch", "codex", "local")
    assert "codex -> local" in event["summary"]
    assert switch_line(1, switch, "en") == "#1 starts with local instead of Codex: Codex paused at its limit until x."


@pytest.mark.parametrize("executor,implementer,reviewer,paused,expected", [
    # [codex, local]: a fallback to another executor keeps the configured reviewer.
    ("codex", "local", None, (), "claude"),
    ("codex", "local", None, ("codex",), "claude"),
    ("codex", "local", "codex", ("codex",), "none"),       # a paused reviewer is skipped; local cannot review
    ("local", "claude", "codex", ("codex",), "claude"),    # [local, claude]: the run's own executor reviews
    ("local", "claude", "codex", (), "codex"),             # a free configured reviewer is kept
    ("local", "codex", "claude", (), "claude"),           # from another executor: kept too
    # [claude, codex]: the executor it fell back from reviews when free, else the run's own.
    ("claude", "codex", None, (), "claude"),
    ("claude", "codex", None, ("claude",), "codex"),
    ("claude", "codex", "none", (), "none"),
    # [codex, claude]: the same rule reversed.
    ("codex", "claude", None, (), "codex"),
    ("codex", "claude", None, ("codex",), "claude"),
    ("codex", "claude", "codex", ("codex",), "claude"),
])
def test_fallback_reviewer(executor, implementer, reviewer, paused, expected):
    config = Config(executor=executor, reviewer=reviewer)
    assert switched_config(config, implementer, available=lambda name: name not in paused).reviewer == expected


def test_unlimited_executors_never_review(tmp_path):
    config = load(tmp_path, {"executors": [entry("codex"), entry("claude", "unlimited")]})
    store = store_at(tmp_path)
    start = decide(store, config, "main", NOW)
    assert (start.config.executor, start.config.reviewer, start.held) == ("codex", "codex", False)


@pytest.mark.parametrize("lane,paused", [("main", ()), ("main", ("codex",)), ("ui", ())])
def test_no_eligible_reviewer_means_no_review(tmp_path, lane, paused):
    config = load(tmp_path, {"executors": [entry("codex", "unlimited"), entry("claude", "unlimited")],
                             "lanes": {"ui": {"executor": "claude"}}})
    store = store_at(tmp_path)
    for name in paused:
        store.pause_executor(name, NOW + timedelta(hours=1))
    start = decide(store, config, lane, NOW)
    assert not start.held and start.config.reviewer == "none", "unlimited executors never review"


def test_fallback_reviewer_is_none_when_no_eligible_reviewer_is_free():
    config = Config(executor="claude", reviewer="codex",
                    executors=[entry("claude"), entry("codex", "unlimited"), entry("local", "unlimited")])
    switched = switched_config(config, "local", available=lambda name: name not in ("claude", "codex"))
    assert switched.reviewer == "none", "the kept reviewer is unlimited and local cannot review"


def test_base_move_check_applies_the_executors_file_rules(monkeypatch):
    from foragent import run_base_move

    def config_at(data):
        monkeypatch.setattr(run_base_move, "_blob_exists", lambda git, sha, path: True)
        monkeypatch.setattr(run_base_move, "_git_bytes", lambda git, args: yaml.safe_dump(data).encode())
        return run_base_move._repo_config(None, "sha")

    assert config_at({"executors": [entry("claude")], "executor": "codex"}) is None
    assert config_at({"executors": [entry("claude")], "claude_quota": {"mode": "burn"}}) is None
    assert config_at({"executors": None}) is None
    assert config_at({"executors": [entry("claude")]}) is not None


def test_reviewer_at_its_spend_cap_holds_a_run_on_its_own_executor(tmp_path):
    config = load(tmp_path, {"reviewer": "claude", "executors": [entry("codex"), entry("claude", "api", spend_cap=2)]})
    store = store_at(tmp_path)
    assert not decide(store, config, "main", NOW).held
    store.record_spend("claude", 2)
    start = decide(store, config, "main", NOW)
    assert start.held and "daily cap" in start.reason


def test_derived_list_lane_does_not_wrap(tmp_path):
    config = load(tmp_path, {"executor": "claude", "reviewer": "none", "lanes": {"ui": {"executor": "codex"}}})
    store = store_at(tmp_path)
    store.pause_executor("codex", NOW + timedelta(hours=1))
    assert decide(store, config, "ui", NOW).held, "a codex lane waits when Codex is paused, as today"


def test_reached_subscription_cap_stays_paused_after_the_pause_ends(tmp_path):
    config = load(tmp_path, {"reviewer": "none", "executors": [entry("claude", spend_cap=3), entry("codex")]})
    store = store_at(tmp_path)
    store.pause_executor("claude", NOW + timedelta(minutes=1))
    store.record_spend("claude", 3)
    later = NOW + timedelta(hours=1)
    assert decide(store, config, "main", later).config.executor == "codex"
    assert decide(store, config, "main", later + timedelta(days=1)).config.executor == "claude"


def test_burn_turns_off_claude_keep_percent_only(tmp_path):
    config = load(tmp_path, {"reviewer": "none", "executors": [
        entry("claude", keep_percent=50), entry("codex", keep_percent=50), entry("local", "unlimited")]})
    store = store_at(tmp_path)
    for name in ("claude", "codex"):
        store.record_usage(name, [{"name": "w", "used_percent": 60, "resets_at": None}])
    assert decide(store, config, "main", NOW).config.executor == "local"
    store.set_claude_quota_mode("burn")
    assert decide(store, config, "main", NOW).config.executor == "claude"
    lane = replace(config, lanes={"x": {"executor": "codex"}})
    assert decide(store, lane, "x", NOW).config.executor == "local", "Codex keeps its keep_percent in burn"


def test_credits_question_is_once_per_executor_and_only_when_it_holds(tmp_path):
    config = load(tmp_path, {"reviewer": "none", "language": "ko", "executors": [entry("codex"), entry("claude")]})
    store = store_at(tmp_path)
    store.pause_executor("codex", NOW + timedelta(hours=1))
    decide(store, config, "main", NOW)
    assert questions(store) == [], "Claude took the start, so Codex's limit held nothing"
    store.pause_executor("claude", NOW + timedelta(hours=1))
    for _ in range(3):
        assert decide(store, config, "main", NOW).held
    asked = questions(store)
    assert sorted(q["key"] for q in asked) == ["credits:claude", "credits:codex"]
    assert asked[0]["options"][0] == "재설정까지 기다리기(추천)"


def result_line(cost):
    return json.dumps({"type": "result", "total_cost_usd": cost})


def test_record_cost_sums_result_lines_and_needs_a_cost_field(tmp_path):
    store = store_at(tmp_path)
    assert record_cost(store, "claude", ["noise", result_line(1.5), result_line(0.5)]) == 2.0
    assert record_cost(store, "claude", [json.dumps({"type": "result"})]) is None
    assert store.spend_today("claude", NOW) == 2.0
    assert store.spend_today("claude", NOW + timedelta(days=1)) == 0.0


def test_credits_is_decided_when_the_session_launches(tmp_path):
    store = store_at(tmp_path)
    plan = load(tmp_path, {"executors": [entry("claude", spend_cap=10)]})
    assert not credits(store, plan, "claude", NOW), "inside the plan is not money"
    store.pause_executor("claude", NOW + timedelta(hours=1))
    assert credits(store, plan, "claude", NOW)
    assert not credits(store, load(tmp_path, {"executors": [entry("claude")]}), "claude", NOW)
    assert credits(store, load(tmp_path, {"executors": [entry("claude", "api", spend_cap=1)]}), "claude", NOW)


def runner_ctx(tmp_path, config):
    from types import SimpleNamespace

    store = store_at(tmp_path)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    return SimpleNamespace(store=store, config=config, now=lambda: NOW)


def test_failed_resume_relaunch_is_admitted_again(tmp_path, monkeypatch):
    from foragent import run_execution
    from foragent.executors import ExecResult
    from foragent.run_attempt import ExecutorStopped

    ctx = runner_ctx(tmp_path, load(tmp_path, {"executors": [entry("claude", "api", spend_cap=1)]}))
    ctx.store.update_run(1, session_id="s1", session_executor="claude")
    launches = []

    def launch(n, context, executor, *, spender=None, **kwargs):
        launches.append(spender)
        context.store.record_spend("claude", 1)  # This paid session reaches the cap.
        return ExecResult(exit_code=1, timed_out=False, log_path=kwargs["log_path"], session_id=None,
                          last_message="resume failed", log_tail="")

    monkeypatch.setattr(run_execution, "_run_executor", launch)
    monkeypatch.setattr(run_execution, "_limit_wait", lambda n, context, detail: (_ for _ in ()).throw(
        ExecutorStopped("limit-wait", detail)))
    monkeypatch.setattr("foragent.runner._remaining", lambda n, context: 60)
    with pytest.raises(ExecutorStopped, match="daily cap"):
        run_execution._available_executor(1, ctx, object(), session_body="body", resume_prompt="go on",
                                          prompt="full", log_path=tmp_path / "log")
    assert launches == ["claude"], "the fresh start after a failed resume must not launch past the cap"


def test_session_gate_stops_at_the_cap_and_lets_credits_past_the_pause(tmp_path):
    from foragent.run_execution import _session_hold

    ctx = runner_ctx(tmp_path, load(tmp_path, {"executors": [entry("claude", spend_cap=4), entry("codex")]}))
    ctx.store.pause_executor("claude", NOW + timedelta(hours=1))
    ctx.store.pause_executor("codex", NOW + timedelta(hours=1))
    assert _session_hold(ctx, "claude", "main") is None, "credits under the cap launch past the pause"
    assert "paused for a usage limit" in _session_hold(ctx, "codex", "main")
    ctx.store.record_spend("claude", 4)
    assert "daily cap" in _session_hold(ctx, "claude", "main"), "a later session stops at the cap"


@pytest.mark.parametrize("name", ["codex", "local"])
def test_codex_api_is_held_because_its_cost_is_not_measured(tmp_path, name):
    config = load(tmp_path, {"reviewer": "none", "executors": [entry(name, "api", spend_cap=5)]})
    start = decide(store_at(tmp_path), config, "main", NOW)
    assert start.held and "cannot be measured" in start.reason


def test_paused_reviewer_that_holds_a_start_records_its_credits_question(tmp_path):
    config = load(tmp_path, {"executor": "claude", "reviewer": "codex", "claude_quota": {"mode": "burn"}})
    store = store_at(tmp_path)
    store.pause_executor("codex", NOW + timedelta(hours=1))
    for _ in range(2):
        start = decide(store, config, "main", NOW)
        assert start.held and start.config.executor == "claude"
    assert [q["key"] for q in questions(store)] == ["credits:codex"]


@pytest.mark.parametrize("outcome", ["crashed", "stalled-restart", "limit-wait"])
def test_continued_run_is_not_held_by_keep_percent(tmp_path, outcome):
    config = load(tmp_path, {"reviewer": "none", "executors": [entry("claude"), entry("codex", keep_percent=50)]})
    store = store_at(tmp_path)
    store.start_run(1, branch="b", worktree=tmp_path / "w", implementer="codex")
    store.finish_run(1, outcome)
    store.record_usage("codex", [{"name": "5h", "used_percent": 90, "resets_at": None}])
    start = decide(store, config, "main", NOW, 1)
    assert (start.held, start.config.executor) == (False, "codex"), "a continuation keeps Codex over its keep share"
    store.pause_executor("codex", NOW + timedelta(hours=1))
    assert decide(store, config, "main", NOW, 1).held, "a hard limit still holds the continuation"


def test_fallback_to_another_executor_keeps_the_configured_reviewer_in_decide(tmp_path):
    config = load(tmp_path, {"reviewer": "claude", "executors": [entry("codex", keep_percent=50),
                                                                 entry("local", "unlimited")]})
    store = store_at(tmp_path)
    store.record_usage("codex", [{"name": "5h", "used_percent": 60, "resets_at": None}])
    start = decide(store, config, "main", NOW)
    assert (start.config.executor, start.config.reviewer, start.held) == ("local", "claude", False)



def test_crashed_run_over_keep_percent_resumes_through_the_tick(ctx):
    """The lane-wide answer (Claude paused, Codex over keep) must not hold a continued Codex run."""
    from foragent.scheduler import tick
    from foragent.scheduler_start import _lane_held

    now = ctx.now()
    ctx.config = replace(ctx.config, executor="claude", reviewer="none",
                         executors=[entry("claude"), entry("codex", keep_percent=50)])
    ctx.start_runs = True
    sched_approve(ctx, 1)
    ctx.store.pause_executor("claude", now + timedelta(hours=1))
    tick(ctx)
    assert ctx.store.get_run(1).implementer == "codex"
    ctx.store.finish_run(1, "crashed")
    ctx.process_table.exit(7001)
    ctx.tracker.remove_labels(1, ["running"])
    ctx.tracker.add_labels(1, ["ready"])
    ctx.store.record_usage("codex", [{"name": "5h", "used_percent": 60, "resets_at": None}])
    assert _lane_held(ctx, "main", now), "a fresh start in this lane waits"
    assert not _lane_held(ctx, "main", now, 1), "the continued run does not"
    tick(ctx)
    run = ctx.store.get_run(1)
    assert run.status == "running" and run.implementer == "codex"


def spy_decide(monkeypatch):
    from foragent import scheduler_quota

    numbers, real = [], scheduler_quota.decide
    monkeypatch.setattr(scheduler_quota, "decide", lambda store, config, lane, now, number=None:
                        numbers.append(number) or real(store, config, lane, now, number))
    return numbers


def test_machine_budget_counts_waiting_issues_by_their_own_quota_decision(ctx, monkeypatch):
    from foragent.scheduler_start import _machine_budget

    numbers = spy_decide(monkeypatch)
    _machine_budget(ctx, {"main": [1, 2]}, ("normal", 0.0))
    assert numbers == [1, 2]


def test_facts_replay_uses_each_issues_own_quota_decision(tmp_path, monkeypatch):
    from foragent import orch_facts_resources
    from foragent.orch_facts_runs import _waiting_issues
    from test_orch_binding_unit import NOW as BINDING_NOW, _store, _waiting

    store = _store(tmp_path)
    _waiting(store, 7, files="src/a.py")
    numbers = spy_decide(monkeypatch)
    config = Config()
    orch_facts_resources._admission(store, config, tmp_path, BINDING_NOW, _waiting_issues(store, config, []), [],
                                    machine={"budget": {"max_parallel": None}, "alive": {}})
    assert numbers == [7]


def test_fallback_from_another_executor_never_waits_for_a_paused_reviewer(tmp_path):
    from foragent.run_implementation import lane_config
    from foragent.scheduler_quota import run_config

    config = load(tmp_path, {"reviewer": "codex", "executors": [entry("local", "unlimited"), entry("claude")]})
    store = store_at(tmp_path)
    for name in ("local", "codex"):
        store.pause_executor(name, NOW + timedelta(hours=1))
    start = decide(store, config, "main", NOW)
    assert (start.config.executor, start.config.reviewer, start.held) == ("claude", "claude", False)
    runner_config = run_config(store, lane_config(config, "main"), start.implementer, NOW)
    assert runner_config.reviewer == "claude", "the runner reviews with the same free executor"
