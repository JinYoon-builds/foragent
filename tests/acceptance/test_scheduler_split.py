"""Acceptance test for scheduler-split. Locked by Foragent; do not edit."""

from pathlib import Path

from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance, split_modules


# Frozen from src/foragent/*.py before the split.
BASE_MODULES = (
    "__init__.py", "actions.py", "adopt.py", "bench.py", "board.py", "board_fake.py",
    "cli.py", "config.py", "design.py", "executors.py", "git.py", "i18n.py", "init.py",
    "inspect_actions.py", "inspect_data.py", "inspect_runtime.py", "issue.py", "kick.py",
    "labels.py", "lanes.py", "launchd.py", "launcher.py", "license_texts.py", "notify.py",
    "orch.py", "orch_events.py", "orch_reinstall.py", "order.py", "paths.py", "policy.py",
    "processes.py", "reasons.py", "runner.py", "scheduler.py", "skills.py", "state.py",
    "status.py", "tracker.py", "trycard.py", "watch.py", "watch_open.py",
)
SCHEDULER_NAMES = (
    "APPROVED", "Approval", "BLOCKED", "Callable", "Config", "GitRepo", "Issue",
    "LabelEvent", "NEEDS_HUMAN", "Path", "READY", "RUNNING", "Reason", "StateStore",
    "TickContext", "TickReport", "Tracker", "TrackerError", "UTC", "_batch_pr",
    "_check_integrity", "_check_prerequisites", "_crash_detail", "_escalate_overdue_blocker",
    "_invalidate_approval", "_kill_executor", "_kill_run", "_latest_approval",
    "_reconcile_batches", "_recover_orphan", "_recover_runs", "_restore_after_crash",
    "_restore_stalled", "_snapshot", "_tick_locked", "acceptance_paths", "append_event",
    "board", "body_hash", "dataclass", "datetime", "field", "flag", "format_approval_comment",
    "format_comment", "has_machine_check", "in_run_hours", "invalidate_approval", "issue_lane",
    "issue_size", "kill_process_group", "lane_order", "os", "parse_body", "re", "replace",
    "runner_process_exists", "subprocess", "text", "tick", "timedelta", "watch_open",
)


def test_scheduler_split_modules_meet_line_limits():
    from foragent import scheduler

    missing = [name for name in SCHEDULER_NAMES if not hasattr(scheduler, name)]
    assert not missing, f"foragent.scheduler lost original bindings: {missing}"


def test_scheduler_split_original_names_remain_importable():
    from foragent import scheduler

    missing = [name for name in SCHEDULER_NAMES if not hasattr(scheduler, name)]
    assert not missing, f"foragent.scheduler lost original bindings: {missing}"


def test_scheduler_split_machine_check_patch_reaches_tick(git_history, tmp_path, monkeypatch):
    from foragent import scheduler
    from foragent.reasons import Reason
    from test_orch_review_handoff import make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    case.store.finish_run(1, "cancelled")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.record_label(1, "approved", "human")
    context = tick_context(case)
    context.start_runs = False
    assert scheduler.tick(context).skipped is None
    assert "ready" in case.tracker.get_issue(1).labels
    context.start_runs = True
    calls = []

    def has_machine_check(parsed):
        calls.append(parsed)
        return len(calls) == 1

    # This exact facade binding is patched in tests/test_scheduler.py.
    monkeypatch.setattr(scheduler, "has_machine_check", has_machine_check)
    assert scheduler.tick(context).skipped is None
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0].goal == "Implement the feature."
    assert case.tracker.get_issue(1).labels == {"approved", "blocked"}
    assert case.store.load_flag(1).reason == Reason.NO_MACHINE_CHECK
    assert case.store.active_runs() == []


def test_scheduler_split_busy_tick_lock_skips_patched_dependencies(
    git_history, tmp_path, monkeypatch,
):
    from foragent import scheduler
    from test_orch_review_handoff import make_case, tick_context

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")

    def unexpected(*args, **kwargs):
        raise AssertionError("A competing tick must return before fetching or checking issues")

    monkeypatch.setattr(scheduler, "has_machine_check", unexpected)
    monkeypatch.setattr(case.git, "fetch", unexpected)
    with case.store.tick_lock() as acquired:
        assert acquired
        report = scheduler.tick(tick_context(case))
    assert report.skipped == "another tick is running"
    assert report.actions == []
