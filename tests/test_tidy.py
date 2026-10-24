from datetime import UTC, datetime, timedelta
import json

from foragent import tidy, usage_events
from foragent.orch_inbox import requests
from foragent.state import StateStore


NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def store_at(tmp_path, clock):
    return StateStore(tmp_path / "state", now=lambda: clock[0])


def merge(store, clock, number, outcome="merged"):
    clock[0] += timedelta(seconds=1)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    store.finish_run(number, outcome=outcome)


def test_idle_quota_needs_a_merge_an_idle_gate_and_quota_left(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    reset = (NOW + timedelta(hours=5)).isoformat()
    store.record_usage("claude", [{"name": "five_hour", "used_percent": 40.0, "resets_at": reset}])
    assert tidy.status(store)["reason"] is None  # Nothing merged since the last tidy.
    merge(store, clock, 1)
    facts = tidy.status(store)
    assert facts["reason"] == "idle-quota"
    assert any("40% used (60% remaining" in line for line in facts["evidence"])
    store.record_usage("claude", [{"name": "five_hour", "used_percent": 100.0, "resets_at": reset}])
    assert tidy.status(store)["reason"] is None
    clock[0] = NOW + timedelta(hours=6)  # The exhausted window has reset since.
    assert tidy.status(store)["reason"] == "idle-quota"


def test_merge_threshold_ignores_other_outcomes_and_done_starts_a_new_period(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    merge(store, clock, 99, outcome="blocked")
    for number in range(1, tidy.MERGE_THRESHOLD + 1):
        merge(store, clock, number)
    first = tidy.post_if_due(store)
    assert first and "merges since the last tidy: 30" in first["text"]
    assert tidy.post_if_due(store) is None
    facts = tidy.done(store)
    assert facts == {"due": False, "reason": None, "merges_since": 0, "last_tidy_at": clock[0].isoformat()}
    for number in range(31, 31 + tidy.MERGE_THRESHOLD):
        merge(store, clock, number)
    second = tidy.post_if_due(store)
    assert second and second["id"] != first["id"]
    assert [row["from"] for row in requests(store)] == ["gate", "gate"]


def test_usage_events_read_both_formats_and_skip_unreadable_lines(tmp_path):
    log = tmp_path / "run.log"
    claude = {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 0.4, "resetsAt": 1792671600}}}}
    codex = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": 23.0, "window_minutes": 300, "resets_at": 1793082762},
        "secondary": None}}}
    log.write_text("plain text\n{unreadable\n" + json.dumps(claude) + "\n" + json.dumps(codex) + "\n")
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    usage_events.record_from_log(store, log)
    usage = store.usage()
    assert usage["claude"]["windows"] == [{"name": "five_hour", "used_percent": 40.0,
                                           "resets_at": datetime.fromtimestamp(1792671600, UTC).isoformat()}]
    assert usage["codex"]["windows"][0]["name"] == "5h"
    assert usage["codex"]["seen_at"] == NOW.isoformat()
    usage_events.record_from_log(store, tmp_path / "missing.log")
    assert store.usage() == usage


def test_failed_inbox_write_does_not_suppress_the_next_attempt(tmp_path, monkeypatch):
    from foragent import orch_inbox

    clock = [NOW]
    store = store_at(tmp_path, clock)
    for number in range(1, tidy.MERGE_THRESHOLD + 1):
        merge(store, clock, number)
    create = orch_inbox._create

    def interrupted(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(orch_inbox, "_create", interrupted)
    try:
        tidy.post_if_due(store)
    except OSError:
        pass
    assert requests(store) == []
    monkeypatch.setattr(orch_inbox, "_create", create)
    posted = tidy.post_if_due(StateStore(store.root, now=lambda: clock[0]))
    assert posted and [row["id"] for row in requests(store)] == [posted["id"]]
    assert tidy.post_if_due(store) is None
