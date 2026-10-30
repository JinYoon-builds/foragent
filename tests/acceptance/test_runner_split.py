"""Acceptance test for runner-split. Locked by Foragent; do not edit."""

from pathlib import Path

import pytest

from test_skills_autosync import isolated_home


# Frozen before the split.
BASE_MODULES = (
    "__init__.py", "actions.py", "adopt.py", "bench.py", "board.py", "board_fake.py",
    "cli.py", "config.py", "design.py", "executors.py", "git.py", "i18n.py", "init.py",
    "inspect_actions.py", "inspect_data.py", "inspect_runtime.py", "issue.py", "kick.py",
    "labels.py", "lanes.py", "launchd.py", "launcher.py", "license_texts.py", "notify.py",
    "orch.py", "orch_events.py", "orch_reinstall.py", "order.py", "paths.py", "policy.py",
    "processes.py", "reasons.py", "runner.py", "scheduler.py", "skills.py", "state.py",
    "status.py", "tracker.py", "trycard.py", "watch.py", "watch_open.py",
)

# Frozen from the original runner.py AST: top-level definitions, assignments and
# import bindings, including private names used by the existing tests.
RUNNER_NAMES = (
    "APPROVED", "AcceptanceBaseline", "Approval", "AttemptFailure", "BLOCKED", "CIFailure",
    "Callable", "CheckResult", "Config", "ConflictBudgetExhausted", "ET", "ExecResult",
    "Executor", "ExecutorStopped", "ExitStack", "GitError", "GitRepo", "NEEDS_HUMAN",
    "ParsedIssue", "PatchReplayError", "Path", "READY", "REVIEW_PROMPT", "REVIEW_SCOPE_FILE",
    "RUNNING", "Reason", "ReviewResult", "RunContext", "SkillManager", "StateStore", "Tracker",
    "TrackerError", "UTC", "_acceptance_matches", "_append", "_available_executor", "_batch_pr",
    "_capture_acceptance_baseline", "_check", "_ci_failure", "_claude_review_scope",
    "_claude_run_dir", "_execute", "_executor_wait", "_hold_changed_head",
    "_implement_and_publish", "_inject_lane_skills", "_lane_notes", "_lane_skill_names",
    "_lane_skills", "_merge_lock", "_next_review_round", "_normalized_findings",
    "_passing_acceptance_ids", "_phase", "_pr_body", "_progress", "_pytest_command",
    "_read_rebuttals", "_remaining", "_review", "_review_only", "_run_executor", "_timeout",
    "_write_claude_settings", "after_merge", "attempt_tier", "board", "body_hash",
    "build_prompt", "build_review_prompt", "check_done", "classify_result", "contextmanager",
    "copy_lane_skills", "dataclass", "datetime", "detect_test_commands", "executor_for", "flag",
    "goal_condition", "has_machine_check", "invalidate_approval", "islice", "issue_lane",
    "issue_size", "json", "kick_tick", "merge_decision", "os", "parse_body", "parse_code_review",
    "parse_review", "poll_executor", "re", "replace", "resolved_reviewer", "review_mode_for",
    "review_only", "review_scope_rules", "run_issue", "run_logged", "shlex", "sys", "tempfile",
    "threading", "time", "timedelta", "touched", "traceback", "track_process_group", "yaml",
)


def test_runner_split_modules_meet_line_limits():
    from foragent import runner

    missing = [name for name in RUNNER_NAMES if not hasattr(runner, name)]
    assert not missing, f"foragent.runner lost original bindings: {missing}"


def test_runner_split_original_names_remain_importable():
    from foragent import runner

    missing = [name for name in RUNNER_NAMES if not hasattr(runner, name)]
    assert not missing, f"foragent.runner lost original bindings: {missing}"


@pytest.mark.parametrize("executor_fails", [False, True], ids=["merged", "executor-error"])
def test_runner_split_kick_patch_reaches_run_issue_even_on_error(
    git_history, tmp_path, monkeypatch, executor_fails,
):
    from foragent import runner
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    calls = []
    # tests/conftest.py patches this facade binding to suppress background ticks.
    monkeypatch.setattr(runner, "kick_tick", calls.append)
    if executor_fails:
        def fail(kwargs):
            raise RuntimeError("offline executor failure")

        case.implementer.steps = [fail]
    outcome = runner.run_issue(1, case.ctx)
    assert outcome == ("error" if executor_fails else "merged"), case.store.log_path(1).read_text()
    assert calls == [case.root]
    assert case.store.get_run(1).status == "finished"
    assert case.store.get_run(1).outcome == outcome


def test_runner_split_after_merge_patch_reaches_run_issue(git_history, tmp_path, monkeypatch):
    from foragent import runner
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    calls = []

    def after_merge(context, pr, base):
        assert case.tracker.merged_prs == [pr]
        calls.append((context.repo_root, pr, base))

    # tests/test_orch.py patches the same binding for post-merge installation.
    monkeypatch.setattr(runner, "after_merge", after_merge)
    assert runner.run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert calls == [(case.root, case.tracker.created_prs[0].number, "main")]
