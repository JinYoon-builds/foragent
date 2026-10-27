"""Boundaries of the Claude keep/burn quota mode (#107)."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest
import yaml

from foragent.config import Config, ConfigError, load_config
from foragent.scheduler import tick
from foragent.scheduler_quota import claude_allowed, claude_percent, switched_config
from foragent.state import StateStore

from test_scheduler import NOW, approve, ctx  # noqa: F401
from test_init import setup  # noqa: F401


def usage(store, percent, *, reset=NOW + timedelta(hours=1), weekly=2.0):
    store.record_usage("claude", [
        {"name": "five_hour", "used_percent": percent, "resets_at": reset.isoformat()},
        {"name": "seven_day", "used_percent": weekly, "resets_at": (NOW + timedelta(days=3)).isoformat()},
    ])


def events(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_keep_mode_starts_claude_lane_with_codex_and_records_why(ctx):
    ctx.config = replace(ctx.config, executor="claude", language="en")
    ctx.start_runs = True
    usage(ctx.store, 81.0)
    approve(ctx, 1)
    report = tick(ctx)
    run = ctx.store.get_run(1)
    assert run.implementer == "codex" and run.pid == 7001
    [event] = [event for event in events(ctx.store) if event["kind"] == "executor-switch"]
    assert (event["issue"], event["from"], event["to"]) == (1, "claude", "codex")
    assert event["reason"] == "Claude keep share 81% >= 80%"
    assert "#1 starts with Codex instead of Claude: Claude keep share 81% >= 80%." in report.actions


@pytest.mark.parametrize("executor,override,percent", [
    ("claude", "burn", 99.0), ("codex", None, 99.0), ("claude", None, 79.0),
])
def test_no_switch_in_burn_mode_for_codex_lanes_or_below_keep(ctx, executor, override, percent):
    ctx.config = replace(ctx.config, executor=executor)
    ctx.start_runs = True
    usage(ctx.store, percent)
    if override:
        ctx.store.set_claude_quota_mode(override)
    approve(ctx, 1)
    tick(ctx)
    assert ctx.store.get_run(1).implementer is None
    assert not [event for event in events(ctx.store) if event["kind"] == "executor-switch"]


def test_switch_waits_when_codex_is_paused_too(ctx):
    ctx.config = replace(ctx.config, executor="claude")
    ctx.start_runs = True
    usage(ctx.store, 90.0)
    ctx.store.pause_executor("codex", NOW + timedelta(hours=1))
    approve(ctx, 1)
    tick(ctx)
    assert ctx.store.get_run(1) is None


def test_lane_pin_is_switched_too(ctx):
    ctx.config = replace(ctx.config, executor="codex", lanes={"ui": {"executor": "claude", "model": "opus"}})
    ctx.start_runs = True
    usage(ctx.store, 95.0)
    approve(ctx, 1, labels=["lane:ui"])
    tick(ctx)
    assert ctx.store.get_run(1).implementer == "codex"


def test_expired_windows_and_unreadable_usage_are_ignored(tmp_path):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    assert claude_allowed(store, Config()) is True
    usage(store, 95.0, reset=NOW - timedelta(minutes=1))
    assert claude_percent(store) == 2.0 and claude_allowed(store, Config()) is True
    (store.root / "usage.json").write_text("{not json")
    assert claude_percent(store) is None and claude_allowed(store, Config()) is True
    (store.root / "claude-quota.json").write_text("[]")
    assert store.claude_quota_mode() is None


@pytest.mark.parametrize("reviewer,paused,expected", [
    (None, False, "claude"), ("codex", False, "claude"), ("claude", False, "claude"),
    ("none", False, "none"), (None, True, "codex"),
])
def test_switched_config_reviews_cross_model_where_possible(reviewer, paused, expected):
    config = Config(executor="claude", executor_model="opus", reviewer=reviewer)
    switched = switched_config(config, "codex", paused)
    assert (switched.executor, switched.executor_model, switched.reviewer) == ("codex", None, expected)
    assert switched_config(config, None) is config and switched_config(config, "claude") is config


def test_runner_uses_the_stored_implementer_for_every_attempt(monkeypatch, tmp_path):
    from foragent import runner

    seen = []
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "w", implementer="codex")
    context = runner.RunContext(repo="owner/repo", repo_root=tmp_path, tracker=None, store=store,
                                config=Config(executor="claude"), git=None, now=lambda: NOW)
    context.git = type("Git", (), {"remove_worktree": lambda self, path: None})()
    monkeypatch.setattr(runner.board, "sync_status", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_execute", lambda n, ctx, worktree: seen.append(ctx.config) or "merged")
    monkeypatch.setattr(runner, "observe_tree", lambda roots: [])
    runner.run_issue(1, context)
    assert seen[0].executor == "codex" and seen[0].reviewer == "claude"
    assert store.get_run(1).implementer == "codex"


@pytest.mark.parametrize("block,key", [
    ({"mode": "keep", "extra": 1}, "claude_quota.extra"), ({"keep_percent": True}, "keep_percent"),
    ({"keep_percent": 80.0}, "keep_percent"), ({"keep_percent": 0}, "keep_percent"),
    ({"keep_percent": 101}, "keep_percent"), ({"mode": None}, "mode"),
])
def test_bad_claude_quota_values_name_the_key(tmp_path, block, key):
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"claude_quota": block}))
    with pytest.raises(ConfigError, match=key.replace(".", r"\.")):
        load_config(tmp_path, global_path=tmp_path / "missing.yml")


def test_repo_block_merges_key_by_key_over_global(tmp_path):
    (tmp_path / "global.yml").write_text(yaml.safe_dump({"claude_quota": {"mode": "burn", "keep_percent": 60}}))
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"claude_quota": {"keep_percent": 70}}))
    assert load_config(tmp_path, global_path=tmp_path / "global.yml").claude_quota == {
        "mode": "burn", "keep_percent": 70}


def test_quota_mode_status_clear_and_conflict(tmp_path, monkeypatch, capsys):
    from foragent import cli

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    usage(store, 42.5)
    monkeypatch.setattr(cli, "load_config", lambda root: Config(repo="owner/repo"))
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: store))
    root = ["--repo-root", str(tmp_path)]
    assert cli.main(["quota", "mode", *root]) == 0
    assert capsys.readouterr().out.strip() == (
        "Claude quota mode: keep (from config); keep stops at 80%; latest Claude usage: 42.5%.")
    assert cli.main(["quota", "mode", "burn", *root]) == 0
    assert json.loads((store.root / "claude-quota.json").read_text()) == {"mode": "burn"}
    assert cli.main(["quota", "mode", "keep", "--clear", *root]) == 2
    assert cli.main(["quota", "mode", "--clear", *root]) == 0
    assert not (store.root / "claude-quota.json").exists()
    capsys.readouterr()
    assert cli.main(["quota", "mode", *root]) == 0
    assert "keep (from config)" in capsys.readouterr().out


def test_init_without_flag_writes_no_block_and_with_flag_keeps_other_keys(setup):
    setup.init()
    path = setup.root / ".foragent.yml"
    assert "claude_quota" not in yaml.safe_load(path.read_text())
    values = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump({**values, "claude_quota": {"keep_percent": 70}}))
    setup.init(claude_quota="burn")
    assert yaml.safe_load(path.read_text())["claude_quota"] == {"keep_percent": 70, "mode": "burn"}


@pytest.mark.parametrize("mode", ["keep", "burn"])
def test_claude_hard_limit_lane_pause_does_not_hold_a_switched_start(ctx, mode):
    from foragent import usage_recheck

    ctx.config = replace(ctx.config, executor="claude", claude_quota={"mode": mode, "keep_percent": 80})
    ctx.start_runs = True
    usage(ctx.store, 100.0)
    until = NOW + timedelta(hours=1)
    usage_recheck.note_limit(ctx.store, "claude", until, [{"name": "five_hour", "exhausted": True}], NOW, lane="main")
    pauses = (ctx.store.root / "lane-pauses.json").read_bytes()
    approve(ctx, 1)
    report = tick(ctx)
    run = ctx.store.get_run(1)
    assert run is not None and run.implementer == "codex"
    reason = f"Claude paused at its limit until {until.isoformat()}"
    assert [event["reason"] for event in events(ctx.store) if event["kind"] == "executor-switch"] == [reason]
    assert any(reason in action for action in report.actions)
    # The tick leaves the Claude-caused lane pause in place.
    assert (ctx.store.root / "lane-pauses.json").read_bytes() == pauses


def test_lane_pause_not_caused_by_claude_still_holds_a_switched_start(ctx):
    ctx.config = replace(ctx.config, executor="claude")
    ctx.start_runs = True
    usage(ctx.store, 90.0)
    ctx.store.pause_lane("main", NOW + timedelta(hours=1))
    approve(ctx, 1)
    tick(ctx)
    assert ctx.store.get_run(1) is None


def test_unswitched_lane_still_waits_on_its_lane_pause(ctx):
    from foragent import usage_recheck

    ctx.config = replace(ctx.config, executor="codex")
    ctx.start_runs = True
    usage_recheck.note_limit(ctx.store, "codex", NOW + timedelta(hours=1), [], NOW, lane="main")
    approve(ctx, 1)
    tick(ctx)
    assert ctx.store.get_run(1) is None


def test_switched_run_limit_waits_without_pausing_the_lane(tmp_path):
    from foragent import usage_recheck

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    until = usage_recheck.note_limit(store, "codex", NOW + timedelta(hours=1), [], NOW, lane=None)
    assert usage_recheck.hold_lane(store, "codex", None, NOW) == until
    assert not store.lane_paused("main", NOW)


@pytest.mark.parametrize("pin,paused,expected", [
    ("none", False, "none"), ("codex", False, "claude"), ("claude", False, "claude"),
    ("claude", True, "codex"), (None, False, "claude"),
])
def test_lane_pinned_reviewer_reaches_the_switched_run(pin, paused, expected):
    from foragent.config import lane_executors
    from foragent.run_implementation import lane_config

    config = Config(executor="codex", lanes={"ui": {"executor": "claude", "reviewer": pin}})
    adjusted = lane_config(config, "ui")
    assert adjusted.reviewer == pin
    assert switched_config(adjusted, "codex", paused).reviewer == expected
    if pin in ("claude", "none"):
        assert lane_executors(config, "ui") == {"claude", pin}


def test_lane_reviewer_value_is_validated(tmp_path):
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"lanes": {"ui": {"reviewer": "gpt"}}}))
    with pytest.raises(ConfigError, match=r"lanes\.ui\.reviewer"):
        load_config(tmp_path, global_path=tmp_path / "missing.yml")


@pytest.mark.parametrize("outcome", ["limit-wait", "crashed", "stalled-restart"])
def test_continued_switched_run_keeps_its_implementer(tmp_path, outcome):
    from foragent.scheduler_quota import run_start

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "w", implementer="codex")
    store.finish_run(1, outcome)
    store.set_claude_quota_mode("burn")  # Claude is allowed again; the run still keeps Codex.
    config, implementer, reason = run_start(store, Config(executor="claude"), "main", 1, NOW)
    assert (config.executor, config.reviewer, implementer, reason) == ("codex", "claude", "codex", None)


def test_finished_run_chooses_afresh(tmp_path):
    from foragent.scheduler_quota import run_start

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="foragent/issue-1", worktree=tmp_path / "w", implementer="codex")
    store.finish_run(1, "merged")
    config, implementer, reason = run_start(store, Config(executor="claude"), "main", 1, NOW)
    assert (config.executor, implementer, reason) == ("claude", None, None)


def test_tick_restart_after_limit_wait_keeps_codex_without_a_new_switch(ctx):
    ctx.config = replace(ctx.config, executor="claude")
    ctx.start_runs = True
    usage(ctx.store, 81.0)
    approve(ctx, 1)
    tick(ctx)
    ctx.store.finish_run(1, "limit-wait")
    ctx.process_table.exit(7001)
    ctx.tracker.remove_labels(1, ["running"])
    ctx.tracker.add_labels(1, ["ready"])
    usage(ctx.store, 5.0)  # Claude's share has reset.
    tick(ctx)
    run = ctx.store.get_run(1)
    assert run.status == "running" and run.implementer == "codex"
    assert len([event for event in events(ctx.store) if event["kind"] == "executor-switch"]) == 1


def test_tick_restart_of_kept_codex_run_waits_while_codex_is_paused(ctx):
    ctx.config = replace(ctx.config, executor="claude")
    ctx.start_runs = True
    usage(ctx.store, 81.0)
    approve(ctx, 1)
    tick(ctx)
    ctx.store.finish_run(1, "limit-wait")
    ctx.process_table.exit(7001)
    ctx.tracker.remove_labels(1, ["running"])
    ctx.tracker.add_labels(1, ["ready"])
    usage(ctx.store, 5.0)
    ctx.store.pause_executor("codex", NOW + timedelta(hours=1))
    tick(ctx)
    assert ctx.store.get_run(1).outcome == "limit-wait"


def test_switch_event_is_written_with_the_run_record(ctx):
    from foragent.scheduler_quota import Snapshot, Switch, switch_journal

    switch = Switch("Claude keep share 90% >= 80%", "switch_reason_keep", {"percent": "90", "keep": 80})
    ctx.store.start_run(1, branch="b", worktree="w", implementer="codex",
                        journal=switch_journal(ctx.store, NOW, 1, switch))
    [event] = [event for event in events(ctx.store) if event["kind"] == "executor-switch"]
    assert event["reason"] == switch.reason and ctx.store.get_run(1).implementer == "codex"
    assert Snapshot(None, 90.0, "keep").claude_paused is False


def test_github_limit_during_start_stores_no_unjournaled_switch_then_next_start_records_it(ctx, monkeypatch):
    from foragent import board
    from foragent.tracker import TrackerError

    ctx.config = replace(ctx.config, executor="claude")
    ctx.start_runs = True
    usage(ctx.store, 85.0)
    approve(ctx, 1)
    calls = []

    def limited(context, issue, status, **kwargs):
        calls.append(status)
        if calls.count("Running") == 1 and status == "Running":
            raise TrackerError("API rate limit exceeded (HTTP 403)",
                               stdout=f"X-RateLimit-Reset: {int((NOW + timedelta(minutes=5)).timestamp())}\r\n")

    monkeypatch.setattr(board, "sync_status", limited)
    tick(ctx)
    run = ctx.store.get_run(1)
    assert run.outcome == "limit-wait" and run.implementer is None
    assert not [event for event in events(ctx.store) if event["kind"] == "executor-switch"]
    from foragent.scheduler_quota import run_start

    assert run_start(ctx.store, ctx.config, "main", 1, NOW)[2] is not None, "the next start still switches"


def test_usage_changing_between_reads_cannot_break_the_decision(ctx, monkeypatch):
    from foragent.scheduler_quota import decide

    ctx.config = replace(ctx.config, executor="claude")
    reads = iter([
        {"claude": {"windows": [{"name": "five_hour", "used_percent": 90.0,
                                 "resets_at": (NOW + timedelta(hours=1)).isoformat()}]}},
    ])
    later = {"claude": {"windows": [{"name": "five_hour", "used_percent": None, "resets_at": None}]}}
    monkeypatch.setattr(ctx.store, "usage", lambda: next(reads, later))
    start = decide(ctx.store, ctx.config, "main", NOW, 1)
    assert start.implementer == "codex" and start.switch.reason == "Claude keep share 90% >= 80%"


def test_korean_report_line_uses_the_registry(ctx):
    ctx.config = replace(ctx.config, executor="claude", language="ko")
    ctx.start_runs = True
    usage(ctx.store, 81.0)
    approve(ctx, 1)
    report = tick(ctx)
    assert "#1은 Claude 대신 Codex로 시작해요: Claude 사용량 81%가 keep 기준 80% 이상이에요" in report.actions
    [event] = [event for event in events(ctx.store) if event["kind"] == "executor-switch"]
    assert event["reason"] == "Claude keep share 81% >= 80%"
