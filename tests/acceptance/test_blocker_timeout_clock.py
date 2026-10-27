"""Acceptance test for blocker-timeout-clock. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta

TIMEOUT = "<!-- foragent:needs-human reason=orchestrator-timeout -->"


def blocked_case(git_history, tmp_path, monkeypatch):
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.config, orchestrator_timeout_minutes=30)
    # An old blocker that the orchestrator already answered with a retry.
    case.tracker.comment(1, "<!-- foragent:blocked reason=risk-high -->\nOld findings")
    case.clock[0] += timedelta(hours=2)
    return case


def block_again(case):
    """The retried run ends blocked now; its new blocker comment has not reached
    the gate's local issue record yet (GitHub reads lag the runner's write)."""
    case.store.finish_run(1, outcome="blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["blocked"])


def escalations(case):
    return [c for c in case.tracker.list_comments(1) if c.body.startswith(TIMEOUT)]


def test_timeout_clock_starts_when_the_latest_run_ends(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case = blocked_case(git_history, tmp_path, monkeypatch)
    block_again(case)
    case.clock[0] += timedelta(minutes=1)
    tick(tick_context(case))
    assert "needs-human" not in case.tracker.get_issue(1).labels
    assert escalations(case) == []

    case.clock[0] += timedelta(minutes=28)
    tick(tick_context(case))
    assert "needs-human" not in case.tracker.get_issue(1).labels

    case.clock[0] += timedelta(minutes=2)
    tick(tick_context(case))
    assert "needs-human" in case.tracker.get_issue(1).labels
    assert len(escalations(case)) == 1


def test_newer_blocked_comment_still_restarts_the_clock(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case = blocked_case(git_history, tmp_path, monkeypatch)
    block_again(case)
    case.clock[0] += timedelta(minutes=20)
    # A later blocker (for example from the gate itself) restarts the clock.
    case.tracker.comment(1, "<!-- foragent:blocked reason=review-dispute -->\nNew dispute")
    case.clock[0] += timedelta(minutes=20)
    tick(tick_context(case))
    assert "needs-human" not in case.tracker.get_issue(1).labels
    case.clock[0] += timedelta(minutes=11)
    tick(tick_context(case))
    assert "needs-human" in case.tracker.get_issue(1).labels
    assert len(escalations(case)) == 1
