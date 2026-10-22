"""Acceptance test for gate-open-view. Locked by Foragent; do not edit."""

import os
from datetime import timedelta


def gate_case(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import TickContext
    from test_view_open import view_case

    case, main, commands = view_case(git_history, tmp_path, monkeypatch)
    # Reuse the review-handoff issue, approval snapshot and real offline Git trees.
    case.store.finish_run(1, outcome="not-started")
    case.tracker.remove_labels(1, ["running", "approved"])
    case.tracker.record_label(1, "approved", "human")
    launched = []
    ctx = TickContext(
        repo="owner/repo", repo_root=case.root, tracker=case.tracker, store=case.store,
        config=case.ctx.config, git=case.git, now=lambda: case.clock[0],
        launcher=lambda number: launched.append(number) or 7000 + number,
        runner_process_exists=lambda number: True, kill_runner=lambda pid: None,
    )
    return case, main, commands, ctx, launched


def test_tick_start_opens_one_terminal_window_without_an_orca_handle(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick
    from test_view_open import assert_opened

    case, main, commands, ctx, launched = gate_case(git_history, tmp_path, monkeypatch)
    report = tick(ctx)
    assert launched == [1] and case.store.get_run(1).pid == 7001, report.actions
    assert_opened(commands, case.root)


def test_tick_start_reuses_the_live_orca_terminal_recorded_by_a_foreground_open(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick
    from test_view_open import assert_opened

    case, main, commands, ctx, launched = gate_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_test1")
    assert main(["gate", "watch", "--open"]) == 0
    assert_opened(commands, case.root, terminal="term_test1")
    commands.clear()
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    report = tick(ctx)
    assert launched == [1], report.actions
    calls = commands.calls("orca")
    assert calls and calls[0]["argv"] == ["terminal", "show", "--terminal", "term_test1"]
    assert_opened(commands, case.root, terminal="term_test1")


def test_live_watch_suppresses_opening_while_runs_still_start_then_reopens_when_gone(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick
    from test_view_open import assert_opened

    case, main, commands, ctx, launched = gate_case(git_history, tmp_path, monkeypatch)
    pid_file = case.store.root / "watch.pid"
    pid_file.write_text(str(os.getpid()))
    report = tick(ctx)
    assert launched == [1] and case.store.get_run(1).pid == 7001, report.actions
    assert commands.calls() == []
    pid_file.unlink()
    case.store.finish_run(1, outcome="merged")
    case.tracker.close_issue(1)
    case.tracker.add_issue(2, "Next task", case.tracker.get_issue(1).body)
    case.tracker.record_label(2, "approved", "human")
    # A person's direct GitHub change is picked up by the next 5-minute refresh (local-truth rule).
    case.clock[0] += timedelta(minutes=5)
    report = tick(ctx)
    assert launched == [1, 2] and case.store.get_run(2).pid == 7002, report.actions
    assert_opened(commands, case.root)


def test_idle_tick_opens_nothing_then_approved_work_opens_the_view(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick
    from test_view_open import assert_opened

    case, main, commands, ctx, launched = gate_case(git_history, tmp_path, monkeypatch)
    case.tracker.remove_labels(1, ["approved"])
    report = tick(ctx)
    assert launched == [] and case.store.active_runs() == [], report.actions
    assert commands.calls() == []
    # Pair the idle contract with a real start so a missing opener cannot pass.
    case.tracker.record_label(1, "approved", "human")
    report = tick(ctx)
    assert launched == [1], report.actions
    assert_opened(commands, case.root)
