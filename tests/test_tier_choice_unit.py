"""Boundaries of the per-attempt tier, model and effort choice (#238)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import quota_headroom
from foragent.config import Config, ConfigError, load_config
from foragent.executors import claude_command
from foragent.run_implementation import ample_quota, choose_tier, tier_model_effort

NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)


class FakeStore:
    def __init__(self, usage, mode=None):
        self._usage, self._mode = usage, mode

    def usage(self):
        return self._usage

    def claude_quota_mode(self):
        return self._mode


def usage(**used):
    reset = (NOW + timedelta(hours=1)).isoformat()
    return {name: {"windows": [{"name": "w", "used_percent": percent, "resets_at": reset}]}
            for name, percent in used.items()}


def ctx(config, used, mode=None):
    return SimpleNamespace(config=config, store=FakeStore(used, mode), now=lambda: NOW)


def run(attempts, tiers=None):
    return SimpleNamespace(attempts=attempts, attempt_tiers=tiers or {})


def test_quota_owner_is_the_run_executor_not_the_reviewer():
    config = Config(executor="codex", reviewer="claude")
    assert ample_quota(ctx(config, usage(codex=10, claude=99)))
    assert not ample_quota(ctx(config, usage(codex=60, claude=0)))


def test_no_current_usage_keeps_the_size_tier():
    config = Config(executor="codex")
    assert not ample_quota(ctx(config, {}))
    assert choose_tier(ctx(config, {}), run(1), "s") == ("1", "s")


def test_api_billed_claude_never_upgrades(monkeypatch):
    config = Config(executor="claude")
    assert ample_quota(ctx(config, usage(claude=0)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert not ample_quota(ctx(config, usage(claude=0)))


def test_burn_lifts_only_claude_quota_keep_percent():
    config = Config(executor="claude")  # keep_percent 80: 35% used leaves 0.45, 1.0 under burn leaves 0.65
    assert not ample_quota(ctx(config, usage(claude=35)))
    assert ample_quota(ctx(config, usage(claude=35), mode="burn"))
    listed = replace(config, executor_list=[{"name": "codex", "quota": "subscription", "keep_percent": 60}])
    assert quota_headroom.cap(listed, "codex", "burn") == 60.0
    assert quota_headroom.cap(listed, "claude", "keep") == 100.0
    listed = replace(listed, executor="codex")
    assert not ample_quota(ctx(listed, usage(codex=15), mode="burn"))  # (60 - 15) / 100 < 0.5
    assert ample_quota(ctx(listed, usage(codex=10), mode="burn"))


def test_threshold_above_one_turns_the_upgrade_off():
    config = Config(executor="codex", tiers_headroom_ample=1.5)
    assert not ample_quota(ctx(config, usage(codex=0)))


@pytest.mark.parametrize("attempts,previous,used,expected", [
    (1, {}, 90, ("1", "s")),                      # tight: turn 1 is the size
    (1, {}, 10, ("1", "l")),                      # ample: turn 1 is l
    (2, {"1": "s"}, 90, ("2", "m")),              # one failed turn steps up once
    (1, {"1": "s"}, 90, ("2", "m")),              # a conflict turn counts like any turn
    (1, {"1": "s", "2": "m"}, 90, ("3", "l")),    # restart after a resolved conflict: never back to s
    (1, {"1": "s", "2": "m", "3": "l"}, 90, ("4", "l")),  # capped at l
    (1, {"1": "s"}, 10, ("2", "m")),              # the base is read once, at turn 1
    (1, {"1": "l"}, 90, ("2", "l")),              # an ample base stays l when quota turns tight
    (1, {"1": "x"}, 10, ("2", "l")),              # no valid turn-1 tier: headroom read now
    (1, {"1": "x"}, 90, ("2", "m")),
])
def test_tier_is_derived_from_the_turn_count_and_turn_one(attempts, previous, used, expected):
    config = Config(executor="codex", max_retries=5)
    assert choose_tier(ctx(config, usage(codex=used)), run(attempts, previous), "s") == expected


def test_a_turn_never_rewrites_a_recorded_key():
    config = Config(executor="codex", max_retries=5)
    for tiers in ({}, {"1": "s"}, {"1": "s", "2": "m"}, {"1": "x", "2": "y", "3": "z"}):
        assert choose_tier(ctx(config, usage(codex=90)), run(1, tiers), "s")[0] not in tiers


@pytest.mark.parametrize("case", ["no-record", "other-executor-only", "expired", "unlimited", "api-claude"])
def test_without_current_usage_turns_step_up_and_the_last_attempt_is_l(case, monkeypatch):
    # The last allowed attempt is l whatever the usage or executor kind (orchestrator, 2026-10-30);
    # before it, an executor without current usage only steps up from its size.
    config = Config(executor="codex", max_retries=2)
    used = {"no-record": {}, "other-executor-only": usage(claude=0)}.get(case, usage(codex=90))
    if case == "expired":
        past = (NOW - timedelta(minutes=1)).isoformat()
        used = {"codex": {"windows": [{"name": "w", "used_percent": 0, "resets_at": past}]}}
    elif case == "unlimited":
        config = replace(config, executor_list=[{"name": "codex", "quota": "unlimited"}])
        used = usage(codex=0)
    elif case == "api-claude":
        config = replace(config, executor="claude")
        used = usage(claude=0)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert choose_tier(ctx(config, used), run(1), "s") == ("1", "s")
    assert choose_tier(ctx(config, used), run(2, {"1": "s"}), "s") == ("2", "m")
    assert choose_tier(ctx(config, used), run(3, {"1": "s", "2": "m"}), "s") == ("3", "l")
    final = replace(config, max_retries=1)
    assert choose_tier(ctx(final, used), run(2, {"1": "s"}), "s") == ("2", "l")


@pytest.mark.parametrize("attempts,previous", [(2, {"1": "s"}), (2, {}), (3, {"1": "s", "2": "m"})])
def test_the_last_allowed_attempt_is_l(attempts, previous):
    config = Config(executor="codex", max_retries=attempts - 1)
    assert choose_tier(ctx(config, usage(codex=90)), run(attempts, previous), "s")[1] == "l"


@pytest.mark.parametrize("tier_model,global_model,lane_model,expected", [
    ("named", "global", None, "named"),
    ("", "global", None, None),
    (None, "global", None, "global"),
    (None, None, None, None),
    ("named", "lane", "lane", "lane"),
])
def test_model_precedence(tier_model, global_model, lane_model, expected):
    tiers = Config().tiers | {"s": {**Config().tiers["s"], "codex_model": tier_model, "codex_effort": "low"}}
    config = Config(executor="codex", executor_model=global_model, tiers=tiers,
                    lanes={"api": {"model": lane_model}} if lane_model else {})
    assert tier_model_effort(config, "api", "s") == (expected, "low")


def test_lane_model_does_not_follow_a_run_moved_to_another_executor():
    config = Config(executor="codex", executor_model=None, lanes={"api": {"model": "opus"}})
    assert tier_model_effort(config, "api", "m") == (None, "high")


def test_other_executor_without_tier_keys_gets_no_flags_but_records_its_tier():
    config = Config(executor="gemini", executor_model="g1", lanes={"api": {"model": "g1"}}, max_retries=5)
    assert tier_model_effort(config, "main", "l") == (None, None)
    assert tier_model_effort(config, "api", "l") == (None, None)
    assert not ample_quota(ctx(config, usage(gemini=0)))
    assert choose_tier(ctx(config, usage(gemini=0)), run(1), "s") == ("1", "s")
    assert choose_tier(ctx(config, usage(gemini=0)), run(2, {"1": "s"}), "s") == ("2", "m")


def test_other_executor_with_tier_keys_uses_them():
    tiers = {name: {**tier, "gemini_model": f"g-{name}", "gemini_effort": None} for name, tier in Config().tiers.items()}
    config = Config(executor="gemini", tiers=tiers)
    assert tier_model_effort(config, "main", "m") == ("g-m", None)
    assert choose_tier(ctx(config, usage(gemini=90)), run(2, {"1": "s"}), "s") == ("2", "m")


@pytest.mark.parametrize("outcome", ["stalled-restart", "crashed", "limit-wait"])
def test_a_restart_keeps_the_turns_so_the_next_turn_adds_one(tmp_path, outcome):
    # Turn 1 ran at l with ample quota; quota is tight after the restart.
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    store.update_run(1, attempts=2, tier="l", attempt_tiers={"1": "l", "2": "l"})
    store.finish_run(1, outcome)
    restarted = store.start_run(1, branch="b", worktree=tmp_path / "w")
    assert (restarted.tier, restarted.attempt_tiers) == ("l", {"1": "l", "2": "l"})
    # #84: only a limit-wait keeps the attempt number.
    assert restarted.attempts == (2 if outcome == "limit-wait" else 1)
    config = Config(executor="codex", max_retries=5)
    assert choose_tier(ctx(config, usage(codex=90)), restarted, "s") == ("3", "l")


def test_a_new_run_after_a_finished_one_starts_from_turn_one(tmp_path):
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    store.update_run(1, attempt_tiers={"1": "s", "2": "m"})
    store.finish_run(1, "merged")
    assert store.start_run(1, branch="b", worktree=tmp_path / "w").attempt_tiers == {}


def test_claude_effort_reaches_the_cli():
    assert claude_command("opus", effort="high")[-4:] == ["--effort", "high", "--model", "opus"]
    assert "--effort" not in claude_command("opus")


@pytest.mark.parametrize("values", [
    {"tiers": {"s": {"claude_effort": "huge"}}},
    {"tiers": {"s": {"codex_effort": "low", "codex_efort": "low"}}},
    {"tiers_headroom_ample": -0.1},
    {"tiers_headroom_ample": True},
])
def test_invalid_tier_settings_are_rejected(tmp_path, values):
    import yaml

    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path, tmp_path / "absent-global.yml")


def test_none_efforts_and_large_threshold_load(tmp_path):
    import yaml

    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump(
        {"tiers": {"s": {"codex_effort": None}}, "tiers_headroom_ample": 2}), encoding="utf-8")
    config = load_config(tmp_path, tmp_path / "absent-global.yml")
    assert config.tiers["s"]["codex_effort"] is None and config.tiers_headroom_ample == 2
