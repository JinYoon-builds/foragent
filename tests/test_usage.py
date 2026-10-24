from datetime import UTC, datetime, timedelta
import json

from foragent.state import StateStore
from foragent.usage_events import limit_reset, parse_usage, record_from_log

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def read_usage(path, now, *, offset=0):
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            return parse_usage(stream.read().decode(), now)
    except FileNotFoundError:
        return {}


def write(path, *events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def test_claude_rejected_without_unified_windows_uses_top_level_reset(tmp_path):
    log = tmp_path / "log"
    reset = int((NOW + timedelta(hours=2)).timestamp())
    write(log, {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": reset, "rateLimitType": "five_hour"}})
    snapshot = read_usage(log, NOW)["claude"]
    assert snapshot == [{"name": "five_hour", "used_percent": None,
                                    "resets_at": datetime.fromtimestamp(reset, UTC).isoformat(),
                                    "exhausted": True}]
    assert limit_reset(snapshot, NOW) == datetime.fromtimestamp(reset, UTC)


def test_codex_secondary_and_relative_reset_and_latest_event_wins(tmp_path):
    log = tmp_path / "log"
    write(log,
          {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
              "primary": {"used_percent": 1, "window_minutes": 300, "resets_in_seconds": 60}}}},
          {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
              "primary": {"used_percent": 40, "window_minutes": 300, "resets_in_seconds": 600},
              "secondary": {"used_percent": 100, "window_minutes": 10080, "resets_in_seconds": 3600}}}})
    snapshot = read_usage(log, NOW)["codex"]
    assert [(w["name"], w["used_percent"], w["exhausted"]) for w in snapshot] == [
        ("5h", 40.0, False), ("7d", 100.0, True)]
    assert limit_reset(snapshot, NOW) == NOW + timedelta(hours=1)


def test_offset_skips_earlier_events_and_garbage_and_past_resets_are_unknown(tmp_path):
    log = tmp_path / "log"
    past = int((NOW - timedelta(minutes=1)).timestamp())
    write(log, {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": past}})
    offset = log.stat().st_size
    with log.open("a") as stream:
        stream.write("{not json\n[1]\n")
    assert read_usage(log, NOW, offset=offset) == {}
    assert limit_reset(read_usage(log, NOW)["claude"], NOW) is None
    assert limit_reset(None, NOW) is None
    assert read_usage(tmp_path / "missing", NOW) == {}


def test_record_from_log_stores_readable_windows_and_returns_exhaustion(tmp_path):
    log = tmp_path / "log"
    reset = int((NOW + timedelta(hours=1)).timestamp())
    write(log, {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": reset, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 0.6, "resetsAt": reset}}}},
        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 100, "window_minutes": 300, "resets_in_seconds": 1e300}}}})
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    parsed = record_from_log(store, log, 0, NOW)
    assert parsed["claude"][0]["exhausted"] and parsed["codex"][0]["exhausted"]
    usage = store.usage()
    assert usage["claude"]["windows"] == [{"name": "five_hour", "used_percent": 60.0,
                                           "resets_at": datetime.fromtimestamp(reset, UTC).isoformat()}]
    assert usage["codex"]["windows"] == [{"name": "5h", "used_percent": 100.0, "resets_at": None}]


def test_claude_rejected_window_without_reset_uses_top_level_reset(tmp_path):
    log = tmp_path / "log"
    reset = int((NOW + timedelta(hours=3)).timestamp())
    write(log, {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": reset, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 0.6}, "seven_day": {"utilization": 0.1}}}})
    snapshot = read_usage(log, NOW)["claude"]
    assert limit_reset(snapshot, NOW) == datetime.fromtimestamp(reset, UTC)
    assert {w["name"]: w["resets_at"] for w in snapshot}["seven_day"] is None


def test_codex_huge_relative_reset_is_unknown_not_an_error(tmp_path):
    log = tmp_path / "log"
    write(log, {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": 100, "window_minutes": 300, "resets_in_seconds": 1e300},
        "secondary": {"used_percent": 100, "window_minutes": 10080, "resets_at": 1e300}}}})
    snapshot = read_usage(log, NOW)["codex"]
    assert [w["resets_at"] for w in snapshot] == [None, None]
    assert limit_reset(snapshot, NOW) is None


def test_unknown_exhausted_reset_waits_for_the_later_of_known_reset_and_fallback():
    known = NOW + timedelta(minutes=5)
    snapshot = [
        {"name": "5h", "exhausted": True, "resets_at": known.isoformat()},
        {"name": "7d", "exhausted": True, "resets_at": None},
        {"name": "1d", "exhausted": False, "resets_at": None},
    ]
    assert limit_reset(snapshot, NOW) is None
    assert limit_reset(snapshot, NOW, NOW + timedelta(minutes=17)) == NOW + timedelta(minutes=17)
    assert limit_reset(snapshot, NOW, NOW + timedelta(minutes=1)) == known
    assert limit_reset(None, NOW, NOW + timedelta(minutes=17)) == NOW + timedelta(minutes=17)


def test_executor_pause_holds_every_lane_that_uses_it(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.scheduler_start import _lane_held

    store = StateStore(tmp_path / "state")
    config = replace(Config(), executor="codex", reviewer=None, lanes={"ui": {"executor": "claude"}, "api": {}})
    ctx = SimpleNamespace(store=store, config=config)
    store.pause_executor("codex", NOW + timedelta(hours=1))
    store.pause_executor("codex", NOW + timedelta(minutes=1))  # never shortened
    later = NOW + timedelta(minutes=30)
    # main and api implement with codex; ui implements with claude and reviews with codex.
    assert _lane_held(ctx, "main", later) and _lane_held(ctx, "api", later) and _lane_held(ctx, "ui", later)
    assert not _lane_held(ctx, "main", NOW + timedelta(hours=1))
    store.pause_executor("claude", NOW + timedelta(hours=2))
    assert _lane_held(ctx, "ui", NOW + timedelta(hours=1)) and _lane_held(ctx, "main", NOW + timedelta(hours=1))
    assert not _lane_held(ctx, "api", NOW + timedelta(hours=2))
