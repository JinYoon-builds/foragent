"""Acceptance test for usage-read. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from test_skills_autosync import isolated_home


CLAUDE_RESET = 1792671600
WEEK_RESET = 1793210400
CODEX_RESET = 1793082762


def usage_event(executor, *, used_percent=6.0, minutes=10080, status="allowed", secondary_percent=2.0):
    if executor == "claude":
        return {"type": "rate_limit_event", "rate_limit_info": {
            "status": status, "resetsAt": CLAUDE_RESET, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": used_percent / 100, "resetsAt": CLAUDE_RESET},
                               "seven_day": {"utilization": secondary_percent / 100, "resetsAt": WEEK_RESET}}}}
    return {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "limit_id": "codex", "primary": {"used_percent": used_percent,
        "window_minutes": minutes, "resets_at": CODEX_RESET}, "secondary": None}}}


def usage_case(git_history, tmp_path, monkeypatch, *, executor="claude", used_percent=6.0,
               minutes=10080, status="allowed", limit=False, unreadable=False, secondary_percent=2.0):
    from foragent.executors import ExecResult
    from foragent.runner import run_issue
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch, executor=executor,
                     reviewer="codex" if executor == "claude" else "claude", skill_libraries=[],
                     limit_wait_minutes=17)
    if executor == "codex":
        case.reviewer.steps = ['{"result": "(none)"}']
    review = case.reviewer.run

    def review_with_usage(**kwargs):
        other = "codex" if executor == "claude" else "claude"
        log = kwargs["log_path"]
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as stream:
            stream.write(json.dumps(usage_event(other, used_percent=67 if other == "codex" else 6)) + "\n")
        return review(**kwargs)

    monkeypatch.setattr(case.reviewer, "run", review_with_usage)

    def execute(**kwargs):
        log = kwargs["log_path"]
        log.parent.mkdir(parents=True, exist_ok=True)
        event = usage_event(executor, used_percent=used_percent, minutes=minutes, status=status,
                            secondary_percent=secondary_percent)
        with log.open("a") as stream:
            stream.write("{unreadable\n" if unreadable else json.dumps(event) + "\n")
        if not limit:
            case.implement(kwargs)
        return ExecResult(1 if limit else 0, False, "usage limit" if limit else "Implemented", log,
                          error_kind="limit" if limit else "normal")

    monkeypatch.setattr(case.implementer, "run", execute)
    assert run_issue(1, case.ctx) == ("limit-wait" if limit else "merged")
    return case


@pytest.mark.parametrize("executor,percent,minutes,window,reset", [
    ("claude", 6.0, 300, "five_hour", CLAUDE_RESET),
    ("codex", 67.0, 10080, "7d", CODEX_RESET),
    ("codex", 23.0, 300, "5h", CODEX_RESET),
    ("codex", 8.5, 90, "90m", CODEX_RESET),
])
def test_usage_read_records_real_executor_events_with_percent_and_iso_reset(
    git_history, tmp_path, monkeypatch, executor, percent, minutes, window, reset,
):
    case = usage_case(git_history, tmp_path, monkeypatch, executor=executor,
                      used_percent=percent, minutes=minutes)
    usage = case.store.usage()[executor]
    assert datetime.fromisoformat(usage["seen_at"]) == case.clock[0]
    windows = {item["name"]: item for item in usage["windows"]}
    assert windows[window]["used_percent"] == pytest.approx(percent)
    assert datetime.fromisoformat(windows[window]["resets_at"]) == datetime.fromtimestamp(reset, UTC)
    if executor == "claude":
        assert windows["seven_day"]["used_percent"] == pytest.approx(2.0)
        assert datetime.fromisoformat(windows["seven_day"]["resets_at"]) == datetime.fromtimestamp(WEEK_RESET, UTC)


def test_usage_read_retains_both_executor_snapshots_from_one_run(git_history, tmp_path, monkeypatch):
    case = usage_case(git_history, tmp_path, monkeypatch)
    usage = case.store.usage()
    assert set(usage) == {"claude", "codex"}
    claude = {window["name"]: window for window in usage["claude"]["windows"]}
    codex = {window["name"]: window for window in usage["codex"]["windows"]}
    assert claude["five_hour"]["used_percent"] == pytest.approx(6)
    assert codex["7d"]["used_percent"] == pytest.approx(67)


@pytest.mark.parametrize("executor,percent,secondary,status,reset", [
    ("claude", 100, 2, "allowed", CLAUDE_RESET), ("claude", 60, 2, "rejected", CLAUDE_RESET),
    ("claude", 6, 100, "allowed", WEEK_RESET), ("codex", 100, 2, "allowed", CODEX_RESET),
])
def test_usage_read_limit_pause_ends_exactly_at_reported_reset(
    git_history, tmp_path, monkeypatch, executor, percent, secondary, status, reset,
):
    case = usage_case(git_history, tmp_path, monkeypatch, executor=executor,
                      used_percent=percent, secondary_percent=secondary, status=status, limit=True)
    until = datetime.fromtimestamp(reset, UTC)
    lane = case.store.get_run(1).lane
    assert case.store.lane_paused(lane, until - timedelta(seconds=1)), "pause ended before reported reset"
    assert not case.store.lane_paused(lane, until), "pause must end exactly at reset"


def test_usage_read_unreadable_limit_uses_configured_wait(git_history, tmp_path, monkeypatch):
    case = usage_case(git_history, tmp_path, monkeypatch, limit=True, unreadable=True)
    until = case.clock[0] + timedelta(minutes=17)
    lane = case.store.get_run(1).lane
    assert case.store.lane_paused(lane, until - timedelta(seconds=1))
    assert not case.store.lane_paused(lane, until)
