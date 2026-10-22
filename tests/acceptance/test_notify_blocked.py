"""Acceptance test for notify-blocked. Locked by Foragent; do not edit."""

from datetime import timedelta

from test_heartbeats import heartbeats_offline, heartbeats_write


def notify_blocked_capture(monkeypatch):
    from foragent import notify

    pushes = []

    def push(config, title, line, link=None):
        pushes.append({"title": title, "line": line, "link": link})
        return True

    monkeypatch.setattr(notify, "send", push)
    return pushes


def test_notify_blocked_exhausted_checks_push_issue_once_across_ticks(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case, tick_context

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    case = make_case(git_history, tmp_path, monkeypatch, language="ko", max_retries=1)
    pushes = notify_blocked_capture(monkeypatch)
    heartbeats_write(case.store, "orch", case.clock[0])

    def incomplete(kwargs):
        (kwargs["worktree"] / "draft.txt").write_text("unfinished\n")

    case.implementer.steps = [incomplete, incomplete]
    assert run_issue(1, case.ctx) == "retries-exceeded"
    assert "blocked" in case.tracker.get_issue(1).labels
    assert case.store.load_flag(1).reason == "retries-exceeded"
    ctx = tick_context(case, start_runs=False)
    tick(ctx)
    assert len(pushes) == 1 and "#1" in pushes[0]["line"], pushes
    for _ in range(3):
        tick(ctx)
    assert len(pushes) == 1, "an unchanged blocker must not repeatedly push"


def test_notify_blocked_orchestrator_outage_pushes_once_and_rearms_after_recovery(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case, tick_context

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    case = make_case(git_history, tmp_path, monkeypatch, language="ko")
    case.tracker.record_label(1, "approved", "human")
    case.store.update_run(1, pid=88001)
    monkeypatch.setattr("foragent.scheduler.os.kill", lambda pid, signal: None)
    pushes = notify_blocked_capture(monkeypatch)
    ctx = tick_context(case, start_runs=False)
    heartbeats_write(case.store, "orch", case.clock[0] - timedelta(seconds=31))
    tick(ctx)
    assert len(pushes) == 1 and "오케스트레이터가 꺼져 있어요" in pushes[0]["line"], pushes
    tick(ctx)
    assert len(pushes) == 1
    heartbeats_write(case.store, "orch", case.clock[0])
    tick(ctx)
    assert len(pushes) == 1
    case.clock[0] += timedelta(seconds=31)
    tick(ctx)
    assert len(pushes) == 2 and "오케스트레이터가 꺼져 있어요" in pushes[-1]["line"]
    tick(ctx)
    assert len(pushes) == 2


def test_notify_blocked_no_outage_push_for_an_idle_project(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case, tick_context

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    case = make_case(git_history, tmp_path, monkeypatch, language="ko")
    case.store.finish_run(1, outcome="merged")
    case.tracker.close_issue(1)
    pushes = notify_blocked_capture(monkeypatch)
    tick(tick_context(case, start_runs=False))
    assert pushes == []
