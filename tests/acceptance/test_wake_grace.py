"""Acceptance test for wake-grace. Locked by Foragent; do not edit."""

from foragent.reasons import Reason
from test_machine_sleep import (  # noqa: F401
    NIGHT, PID, WAKE_GRACE_SECONDS, WAKE_THRESHOLD_SECONDS, assert_running, no_real_sleep,
    sleep_case, tick_case,
)


def wake_with_overdue_progress(git_history, tmp_path, monkeypatch):
    case = sleep_case(git_history, tmp_path, monkeypatch, max_runtime_minutes=2000)
    case.store.update_run(1, restarts=1)
    case.facts.advance(4 * 60)  # Already older than stall_minutes when the laptop sleeps.
    case.facts.advance(NIGHT, asleep=True)
    assert NIGHT > WAKE_THRESHOLD_SECONDS
    tick_case(case)
    assert_running(case, "The first wake tick must defer stalls even for pre-sleep stale progress")
    return case


def assert_stalled(case):
    run = case.store.get_run(1)
    assert run.status == "finished" and run.outcome == "stalled" and case.killed == [PID], (
        "A genuinely silent runner must still be flagged after wake protection ends"
    )
    assert case.store.load_flag(1).reason == Reason.STALLED
    assert "blocked" in case.tracker.get_issue(1).labels


def test_wake_grace_survives_ticks_but_expires_for_a_silent_run(git_history, tmp_path, monkeypatch):
    case = wake_with_overdue_progress(git_history, tmp_path, monkeypatch)
    case.facts.advance(WAKE_GRACE_SECONDS - 1)
    tick_case(case)
    assert_running(case, "Wake grace must survive fresh contexts and must not end before five minutes")
    case.facts.advance(2)
    tick_case(case)
    assert_stalled(case)


def test_only_sleep_above_the_wake_threshold_defers_stalls(git_history, tmp_path, monkeypatch):
    for seconds in (0, WAKE_THRESHOLD_SECONDS, WAKE_THRESHOLD_SECONDS + 1):
        with monkeypatch.context() as patch:
            case = sleep_case(git_history, tmp_path / str(seconds), patch, max_runtime_minutes=2000)
            case.store.update_run(1, restarts=1)
            case.facts.advance(4 * 60)
            case.facts.advance(seconds, asleep=True)
            tick_case(case)
            if seconds > WAKE_THRESHOLD_SECONDS:
                assert_running(case, "Sleep above 60 seconds must start wake grace")
            else:
                assert_stalled(case)


def test_post_wake_activity_ends_grace_before_its_deadline(git_history, tmp_path, monkeypatch):
    case = wake_with_overdue_progress(git_history, tmp_path, monkeypatch)
    case.facts.advance(30)
    case.store.update_run(1, last_progress_at=case.clock[0].isoformat(),
                          last_activity_at=case.clock[0].isoformat())
    tick_case(case)
    assert_running(case, "Post-wake activity must resume ordinary stall accounting")
    case.facts.advance(case.config.stall_minutes * 60 + 1)
    assert 30 + case.config.stall_minutes * 60 + 1 < WAKE_GRACE_SECONDS
    tick_case(case)
    assert_stalled(case)
