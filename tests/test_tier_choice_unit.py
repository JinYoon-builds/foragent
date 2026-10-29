"""Boundaries of the per-attempt tier, model and effort choice (#238)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import quota_headroom
from foragent.config import Config, ConfigError, load_config
from foragent.executors import claude_command
from foragent.run_implementation import ample_quota, choose_tier, tier_model_effort, turn_key

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


def run(attempts, tiers=None, open_turn=None):
    return SimpleNamespace(attempts=attempts, attempt_tiers=tiers or {}, open_turn=open_turn)


def test_quota_owner_is_the_run_executor_not_the_reviewer():
    config = Config(executor="codex", reviewer="claude")
    assert ample_quota(ctx(config, usage(codex=10, claude=99)))
    assert not ample_quota(ctx(config, usage(codex=60, claude=0)))


def test_no_current_usage_keeps_the_size_tier():
    config = Config(executor="codex")
    assert not ample_quota(ctx(config, {}))
    assert choose_tier(ctx(config, {}), run(1), "s") == "s"


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


@pytest.mark.parametrize("attempts,previous,expected", [
    (1, {}, "s"), (2, {"1": "s"}, "m"),
    (2, {"1": "l"}, "l"),          # never below an earlier attempt
    (3, {"1": "s", "2": "m"}, "l"),  # the last allowed attempt is l
    (2, {"1": "s", "2": "s"}, "s"),  # the open (resumed) attempt keeps its stored tier
])
def test_tier_steps_up_and_keeps_resumed_and_earlier_tiers(attempts, previous, expected):
    config = Config(executor="codex", max_retries=2)
    chosen = run(attempts, previous, open_turn=str(attempts) if str(attempts) in previous else None)
    assert choose_tier(ctx(config, usage(codex=90)), chosen, "s") == expected


def test_the_same_attempt_number_keeps_its_stored_tier_even_after_an_answer():
    # A crash after the answer but before checks redoes the same attempt (#238).
    config = Config(executor="codex", max_retries=2)
    assert choose_tier(ctx(config, usage(codex=10)), run(2, {"1": "s", "2": "m"}), "s") == "m"


def test_ample_quota_starts_small_issues_at_l():
    config = Config(executor="codex")
    assert choose_tier(ctx(config, usage(codex=10)), run(1), "s") == "l"


def test_last_allowed_attempt_reaches_l_even_when_the_budget_skips_m():
    config = Config(executor="codex", max_retries=1)
    assert choose_tier(ctx(config, usage(codex=90)), run(2, {"1": "s"}), "s") == "l"


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


def test_other_executor_without_tier_keys_gets_no_flags_and_stays_at_its_size():
    config = Config(executor="gemini", executor_model="g1", lanes={"api": {"model": "g1"}})
    assert tier_model_effort(config, "main", "l") == (None, None)
    assert tier_model_effort(config, "api", "l") == (None, None)
    assert not ample_quota(ctx(config, usage(gemini=0)))
    for attempts, previous in ((1, {}), (2, {"1": "s"}), (3, {"1": "s", "2": "s"})):
        assert choose_tier(ctx(replace(config, max_retries=2), usage(gemini=0)), run(attempts, previous), "s") == "s"


def test_other_executor_with_tier_keys_uses_them():
    tiers = {name: {**tier, "gemini_model": f"g-{name}", "gemini_effort": None} for name, tier in Config().tiers.items()}
    config = Config(executor="gemini", tiers=tiers)
    assert tier_model_effort(config, "main", "m") == ("g-m", None)
    assert choose_tier(ctx(config, usage(gemini=90)), run(2, {"1": "s"}), "s") == "m"


@pytest.mark.parametrize("key,previous,final,expected", [
    ("1c1", {"1": "s"}, False, "m"),              # a conflict turn steps up within the same attempt
    ("1c2", {"1": "s", "1c1": "m"}, False, "l"),  # the third turn of three allowed is the last
    ("1c1", {"1": "s", "1c1": "s"}, False, "s"),  # the open conflict turn keeps its tier
    ("2", {"1": "s"}, True, "l"),                 # a rebuilt attempt is final
])
def test_conflict_and_rebuild_turns_count_as_failed_turns(key, previous, final, expected):
    config = Config(executor="codex", max_retries=2)
    attempts = int(key.partition("c")[0])
    chosen = run(attempts, previous, open_turn=key if key in previous else None)
    assert choose_tier(ctx(config, usage(codex=90)), chosen, "s", key, final=final) == expected


@pytest.mark.parametrize("record,expected", [
    (run(1, {"1": "s"}), "1c1"),
    (run(1, {"1": "s", "1c1": "m"}), "1c2"),
    (run(1, {"1": "s", "1c1": "m", "1c2": "l"}, open_turn="1c2"), "1c2"),  # resumed after a limit wait
    (run(1, {"1": "s", "1c1": "m", "1c2": "l"}), "1c3"),                   # never a stored, answered turn
    (run(2, {"1": "s", "1c1": "m"}, open_turn="1c1"), "2c1"),              # another attempt's open turn
])
def test_conflict_turn_key_comes_from_the_stored_record(record, expected):
    assert turn_key(record, conflict=True) == expected
    assert turn_key(record, conflict=False) == str(record.attempts)


def test_limit_wait_restart_keeps_the_open_turn(tmp_path):
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    store.update_run(1, attempt_tiers={"1": "s", "1c1": "m", "1c2": "l"}, open_turn="1c2")
    store.finish_run(1, "limit-wait")
    resumed = store.start_run(1, branch="b", worktree=tmp_path / "w")
    assert turn_key(resumed, conflict=True) == "1c2"
    store.finish_run(1, "merged")
    fresh = store.start_run(1, branch="b", worktree=tmp_path / "w")
    assert (fresh.open_turn, fresh.attempt_tiers, fresh.tier) == (None, {}, "m")


@pytest.mark.parametrize("outcome", ["stalled-restart", "crashed", "limit-wait"])
def test_gate_restart_keeps_the_open_attempts_tier_under_tight_quota(tmp_path, outcome):
    # Attempt 1 started at l with ample quota; after a restart the quota is tight.
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="b", worktree=tmp_path / "w", tier="s")
    store.update_run(1, tier="l", attempt_tiers={"1": "l"}, open_turn="1")
    store.finish_run(1, outcome)
    restarted = store.start_run(1, branch="b", worktree=tmp_path / "w", tier="s")
    assert (restarted.attempts, restarted.tier, restarted.attempt_tiers) == (1, "l", {"1": "l"})
    config = Config(executor="codex", max_retries=2)
    assert choose_tier(ctx(config, usage(codex=90)), restarted, "s", turn_key(restarted, conflict=False)) == "l"


@pytest.mark.parametrize("outcome", ["stalled-restart", "crashed"])
def test_restart_during_a_later_attempt_resumes_it_then_steps_up(tmp_path, outcome):
    # Crash during attempt 2 at m: the restart is attempt 2 at m; its failure leads to attempt 3 at l.
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    store.update_run(1, attempts=2, attempt_tiers={"1": "s", "2": "m"}, open_turn="2")
    store.finish_run(1, outcome)
    restarted = store.start_run(1, branch="b", worktree=tmp_path / "w")
    assert restarted.attempts == 2
    config = Config(executor="codex", max_retries=5)
    tight = ctx(config, usage(codex=90))
    assert choose_tier(tight, restarted, "s", turn_key(restarted, conflict=False)) == "m"
    failed = store.update_run(1, attempts=3, open_turn=None)
    assert turn_key(failed, conflict=False) == "3"
    assert choose_tier(tight, failed, "s", "3") == "l"


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
