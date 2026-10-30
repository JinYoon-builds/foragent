"""Acceptance test for run-publish-split. Locked by Foragent; do not edit."""

from importlib import import_module
from pathlib import Path

from test_state_split import split_modules


# Frozen from src/foragent/*.py before the split (2026-10-26).
BASE_MODULES = ('__init__.py', '_build.py', 'actions.py', 'adopt.py', 'bench.py', 'board.py', 'board_fake.py', 'brief.py', 'capacity.py', 'ci_local.py', 'ci_mode.py', 'ci_pytest.py', 'ci_shard.py', 'ci_transport.py', 'ci_workflow.py', 'cli.py', 'config.py', 'config_validation.py', 'design.py', 'design_session.py', 'executors.py', 'git.py', 'i18n.py', 'init.py', 'inspect_actions.py', 'inspect_data.py', 'inspect_runtime.py', 'issue.py', 'issue_metadata.py', 'kick.py', 'labels.py', 'lanes.py', 'launchd.py', 'launcher.py', 'license_texts.py', 'machine_budget.py', 'notify.py', 'orch.py', 'orch_events.py', 'orch_github.py', 'orch_inbox.py', 'orch_queue.py', 'orch_reinstall.py', 'orch_tick_outcome.py', 'order.py', 'order_files.py', 'overlap_files.py', 'path_overlap.py', 'paths.py', 'policy.py', 'processes.py', 'reasons.py', 'records.py', 'review_perspectives.py', 'risks.py', 'run_attempt.py', 'run_base_move.py', 'run_baseline.py', 'run_checks.py', 'run_conflicts.py', 'run_execution.py', 'run_implementation.py', 'run_lane_skills.py', 'run_overlap.py', 'run_pr_body.py', 'run_prompts.py', 'run_publish.py', 'run_review.py', 'run_skill_catalog.py', 'run_waits.py', 'runner.py', 'runner_doctor.py', 'scheduler.py', 'scheduler_approval.py', 'scheduler_checks.py', 'scheduler_config.py', 'scheduler_lanes.py', 'scheduler_recovery.py', 'scheduler_start.py', 'scheduler_stop.py', 'skills.py', 'star.py', 'state.py', 'state_base.py', 'state_issues.py', 'state_local.py', 'state_locks.py', 'state_processes.py', 'state_records.py', 'state_refresh.py', 'state_runs.py', 'status.py', 'statusline.py', 'tidy.py', 'tracker.py', 'trycard.py', 'update_check.py', 'usage_events.py', 'usage_recheck.py', 'usage_sources.py', 'watch.py', 'watch_art.py', 'watch_cells.py', 'watch_data.py', 'watch_layout.py', 'watch_mouse.py', 'watch_open.py', 'watch_terminal.py')
# Every module-level name of foragent.run_publish before the split.
RUN_PUBLISH_NAMES = ('APPROVED', 'Approval', 'AttemptFailure', 'BLOCKED', 'BaseMove', 'CIFailure', 'Callable', 'ComparisonEvidence', 'Decision', 'ExecutorStopped', 'ExitStack', 'FlagContext', 'GitError', 'GitRepo', 'ParsedIssue', 'Path', 'PublicationError', 'READY', 'REUSED', 'REVERIFY', 'RUNNING', 'Reason', 'Run', 'TYPE_CHECKING', 'TrackerError', '_CIWait', '_MergeInputs', '_acceptance_matches', '_append', '_batch_pr', '_check_publication', '_ci_failure', '_ci_status', '_credited_wait', '_final_merge_inputs', '_hold_changed_head', '_hold_if_not_started', '_merge_lock', '_open_or_update_pr', '_phase', '_policy_allows', '_pr_body', '_preserve_failed_implementation', '_progress', '_publish', '_publish_locked', '_publish_reviewed_head', '_push_branch', '_render_pr_body', '_risk_always_human', '_risk_notes', '_skills_line', '_timeout', '_update_open_pr', '_wait_for_ci', 'annotations', 'base_move_is_neutral', 'board', 'body_hash', 'capture_move', 'commit_exclusions', 'contextmanager', 'dataclass', 'enabled', 'fetch_move', 'field', 'flag', 'invalidate_approval', 'merge_decision', 'replace', 'report_line', 'require_comparison', 'retitle_for_merge', 'review_mode_for', 'review_perspectives', 'review_scope_rules', 'risks', 'tempfile', 'touched', 'wait_for_overlap')


def test_run_publish_split_modules_meet_line_limits():
    package = Path(__file__).resolve().parents[2] / "src/foragent"
    new = split_modules(package, "run_publish.py", BASE_MODULES)
    assert new, "run_publish.py must import the modules it was split into"
    module = import_module("foragent.run_publish")
    missing = [name for name in RUN_PUBLISH_NAMES if not hasattr(module, name)]
    assert missing == [], f"foragent.run_publish lost names other code and tests use: {missing}"


def test_run_publish_split_original_names_remain_importable():
    module = import_module("foragent.run_publish")
    missing = [name for name in RUN_PUBLISH_NAMES if not hasattr(module, name)]
    assert missing == [], f"foragent.run_publish lost names other code and tests use: {missing}"
