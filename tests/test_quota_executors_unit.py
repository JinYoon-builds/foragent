"""Boundaries of the ordered executors list (#786) beyond the acceptance test."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json

import pytest
import yaml

from foragent.config import Config, ConfigError, load_config
from foragent.orch_inbox import questions
from foragent.scheduler_quota import Switch, decide, record_cost, switch_journal, switch_line, switched_config
from foragent.state import StateStore

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
    ({"executors": [entry("claude"), entry("claude")]}, "twice"),
    ({"executors": [entry("local", "unlimited", keep_percent=50)]}, "keep_percent"),
    ({"executors": [entry("local", "unlimited", spend_cap=5)]}, "spend_cap"),
    ({"executors": [entry("claude", "api", spend_cap=5, keep_percent=50)]}, "keep_percent"),
    ({"executors": [entry("claude", keep_percent=0)]}, "keep_percent"),
    ({"executors": [entry("claude", spend_cap=0)]}, "spend_cap"),
    ({"executors": [entry("local", "api", spend_cap=5)]}, "reports cost"),
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


@pytest.mark.parametrize("executor,implementer,reviewer,expected", [
    ("claude", "codex", None, "claude"),   # fell back to its own reviewer: the source reviews
    ("codex", "claude", "codex", "codex"),  # configured reviewer is not the target: kept
    ("codex", "local", None, "claude"),    # a run on another executor keeps the configured reviewer
    ("local", "codex", "codex", "codex"),  # the source is not claude or codex: kept
])
def test_fallback_reviewer(executor, implementer, reviewer, expected):
    config = Config(executor=executor, reviewer=reviewer)
    assert switched_config(config, implementer).reviewer == expected


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


def test_spend_is_recorded_only_for_api_runs_and_credits_past_a_pause(tmp_path):
    store = store_at(tmp_path)
    api = load(tmp_path, {"executors": [entry("claude", "api", spend_cap=10)]})
    assert record_cost(store, api, NOW.isoformat(), ["noise", result_line(1.5), result_line(0.5)]) == 2.0
    assert record_cost(store, api, NOW.isoformat(), [json.dumps({"type": "result"})]) is None
    plan = load(tmp_path, {"executors": [entry("claude", spend_cap=10)]})
    assert record_cost(store, plan, NOW.isoformat(), [result_line(3)]) is None, "inside the plan is not money"
    store.pause_executor("claude", NOW + timedelta(hours=1))
    assert record_cost(store, plan, NOW.isoformat(), [result_line(3)]) == 3.0
    uncapped = load(tmp_path, {"executors": [entry("claude")]})
    assert record_cost(store, uncapped, NOW.isoformat(), [result_line(3)]) is None
    assert store.spend_today("claude", NOW) == 5.0
    assert store.spend_today("claude", NOW + timedelta(days=1)) == 0.0


def test_codex_api_is_held_because_its_cost_is_not_measured(tmp_path):
    config = load(tmp_path, {"reviewer": "none", "executors": [entry("codex", "api", spend_cap=5)]})
    assert decide(store_at(tmp_path), config, "main", NOW).held
