from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import usage_recheck
from foragent.config import Config
from foragent.state import StateStore

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


@pytest.mark.parametrize("windows,exhausted,expected", [
    ([{"name": "5h", "used_percent": 23}], [{"name": "5h", "used_percent": 100}], True),
    ([{"name": "5h", "used_percent": 100}], [], False),
    ([{"name": "5h", "used_percent": 90}], [{"name": "5h", "used_percent": 60}], False),
    ([{"name": "5h", "used_percent": 59}], [{"name": "5h", "used_percent": 60}], True),
    ([{"name": "5h", "used_percent": 1}], [{"name": "7d", "used_percent": 100}], False),
    ([{"name": "five_hour", "used_percent": 1}], [{"name": "limit", "used_percent": None}], True),
    ([{"name": "five_hour", "used_percent": 100}], [{"name": "limit", "used_percent": None}], False),
])
def test_recovered_needs_every_exhausted_window_to_drop(windows, exhausted, expected):
    assert usage_recheck.recovered(windows, exhausted) is expected


def snapshot(tmp_path, monkeypatch, value):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = tmp_path / "foragent/claude-rate-limits.json"
    path.parent.mkdir(parents=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize("seen,since,readable", [
    (NOW, None, True), (NOW - timedelta(minutes=20), None, True),
    (NOW - timedelta(minutes=20, seconds=1), None, False), (NOW + timedelta(seconds=1), None, False),
    (NOW - timedelta(minutes=1), NOW, False),
])
def test_claude_snapshot_freshness_uses_seen_at(tmp_path, monkeypatch, seen, since, readable):
    snapshot(tmp_path, monkeypatch, {"seen_at": seen.isoformat(),
                                     "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 1}}})
    windows = usage_recheck.read_claude(NOW, timedelta(minutes=10), since)
    assert windows == ([{"name": "five_hour", "used_percent": 5}] if readable else None)


@pytest.mark.parametrize("value", ["{bad", {"seen_at": NOW.isoformat()}, {"seen_at": "x", "rate_limits": {}}])
def test_claude_snapshot_malformed_is_unknown(tmp_path, monkeypatch, value):
    snapshot(tmp_path, monkeypatch, value)
    assert usage_recheck.read_claude(NOW, timedelta(minutes=10), None) is None


def test_codex_missing_binary_is_unknown(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert usage_recheck.read_codex(NOW) is None


def test_recovery_clears_only_lanes_that_use_the_executor(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    until = NOW + timedelta(hours=2)
    store.pause_executor("codex", until)
    store.pause_lane("main", until)
    store.pause_lane("ui", until)
    store.pause_lane("later", until + timedelta(hours=1))
    usage_recheck.note_limit(store, "codex", until, [{"name": "5h", "used_percent": 100, "exhausted": True}], NOW)
    config = Config(executor="codex", reviewer="codex",
                    lanes={"ui": {"executor": "claude"}, "later": {}})
    ctx = SimpleNamespace(store=store, config=config, now=lambda: NOW + timedelta(minutes=10))
    monkeypatch.setattr(usage_recheck, "read_codex", lambda now: [{"name": "5h", "used_percent": 3}])
    assert usage_recheck.recheck(ctx) == ["codex"]
    assert not store.executor_paused("codex", ctx.now())
    assert not store.lane_paused("main", ctx.now())
    assert not store.lane_paused("ui", ctx.now())  # Its reviewer is codex.
    assert store.lane_paused("later", ctx.now())  # A later pause came from elsewhere.
    assert usage_recheck.recheck(ctx) == []


def test_claude_rejection_without_a_named_window_recovers_from_the_snapshot(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    until = NOW + timedelta(hours=2)
    store.pause_executor("claude", until)
    usage_recheck.note_limit(store, "claude", until, [{"name": "limit", "used_percent": None, "exhausted": True}], NOW)
    ctx = SimpleNamespace(store=store, config=Config(executor="claude", reviewer="claude"),
                          now=lambda: NOW + timedelta(minutes=10))
    monkeypatch.setattr(usage_recheck, "read_claude", lambda now, interval, since: [
        {"name": "five_hour", "used_percent": 12}, {"name": "seven_day", "used_percent": 3}])
    assert usage_recheck.recheck(ctx) == ["claude"]
    assert not store.executor_paused("claude", ctx.now())


@pytest.mark.parametrize("renewal", ["later-until", "same-until-new-note"])
def test_pause_renewed_during_the_read_is_kept(tmp_path, monkeypatch, renewal):
    store = StateStore(tmp_path / "state")
    until = NOW + timedelta(hours=2)
    store.pause_executor("codex", until)
    usage_recheck.note_limit(store, "codex", until, [{"name": "5h", "used_percent": 100, "exhausted": True}], NOW)
    ctx = SimpleNamespace(store=store, config=Config(executor="codex", reviewer="codex"),
                          now=lambda: NOW + timedelta(minutes=10))
    renewed = until + timedelta(hours=1) if renewal == "later-until" else until

    def read(now):
        # Another runner hits the limit again while the free source is answering.
        store.pause_executor("codex", renewed)
        usage_recheck.note_limit(store, "codex", renewed,
                                 [{"name": "5h", "used_percent": 100, "exhausted": True}], ctx.now())
        return [{"name": "5h", "used_percent": 3}]

    monkeypatch.setattr(usage_recheck, "read_codex", read)
    assert usage_recheck.recheck(ctx) == []
    assert store.executor_pause_until("codex", ctx.now()) == renewed
    assert not (store.root / "orch-events.jsonl").exists()


def test_repeated_limit_with_earlier_reset_keeps_the_strictest_baseline(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    later, earlier = NOW + timedelta(hours=3), NOW + timedelta(hours=1)
    # Claude rejected at 60% first; a second limit announces an earlier reset.
    usage_recheck.note_limit(store, "claude", later, [{"name": "five_hour", "used_percent": 60, "exhausted": True}], NOW)
    store.pause_executor("claude", later)
    usage_recheck.note_limit(store, "claude", earlier,
                             [{"name": "seven_day", "used_percent": 100, "exhausted": True}], NOW)
    store.pause_executor("claude", earlier)
    assert store.executor_pause_until("claude", NOW) == later
    ctx = SimpleNamespace(store=store, config=Config(executor="claude", reviewer="claude"),
                          now=lambda: NOW + timedelta(minutes=10))
    reading = [{"name": "five_hour", "used_percent": 90}, {"name": "seven_day", "used_percent": 2}]
    monkeypatch.setattr(usage_recheck, "read_claude", lambda now, interval, since: reading)
    assert usage_recheck.recheck(ctx) == []
    assert store.executor_pause_until("claude", ctx.now()) == later
    reading[0]["used_percent"] = 50
    ctx.now = lambda: NOW + timedelta(minutes=20)
    assert usage_recheck.recheck(ctx) == ["claude"]


def test_limit_note_and_pause_are_written_together(tmp_path):
    store = StateStore(tmp_path / "state")
    until = NOW + timedelta(hours=2)
    usage_recheck.note_limit(store, "codex", until, [{"name": "5h", "used_percent": 100, "exhausted": True}], NOW)
    assert store.executor_pause_until("codex", NOW) == until
    usage_recheck.note_limit(store, "codex", NOW + timedelta(hours=1), [], NOW)
    assert store.executor_pause_until("codex", NOW) == until  # The later deadline wins.


@pytest.mark.parametrize("renewal", ["later-until", "same-until"])
def test_limit_note_written_between_the_pause_read_and_the_mark_survives(tmp_path, monkeypatch, renewal):
    store = StateStore(tmp_path / "state")
    until = NOW + timedelta(hours=2)
    usage_recheck.note_limit(store, "codex", until, [{"name": "5h", "used_percent": 100, "exhausted": True}], NOW)
    renewed = until + timedelta(hours=1) if renewal == "later-until" else until
    ctx = SimpleNamespace(store=store, config=Config(executor="codex", reviewer="codex"),
                          now=lambda: NOW + timedelta(minutes=10))
    original, writes = store.executor_pause_until, []

    def interleaved(executor, now):
        # Another runner hits a new limit after recheck read the pauses, before it marks them.
        if not writes:
            writes.append(1)
            usage_recheck.note_limit(store, "codex", renewed,
                                     [{"name": "7d", "used_percent": 100, "exhausted": True}], now)
        return original(executor, now)

    reads = []
    monkeypatch.setattr(store, "executor_pause_until", interleaved)
    monkeypatch.setattr(usage_recheck, "read_codex", lambda now: reads.append(now) or [
        {"name": "5h", "used_percent": 3}, {"name": "7d", "used_percent": 100}])
    assert usage_recheck.recheck(ctx) == []
    note = json.loads((store.root / "usage-recheck.json").read_text())["codex"]
    assert {window["name"] for window in note["exhausted"]} == {"5h", "7d"}
    assert note["until"] == renewed.isoformat()
    assert original("codex", ctx.now()) == renewed
    # A later deadline is a newer view than this tick's, so it waits; the same deadline is checked now.
    assert len(reads) == (0 if renewal == "later-until" else 1)
