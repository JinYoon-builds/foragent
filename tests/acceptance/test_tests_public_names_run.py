"""Run/scheduler/baseline unit tests retain coverage through public Foragent names.

The inventory records origin/main's test names and parameter counts. As in the
view acceptance tests, run the real unit files and break behaviour in disposable
source copies; never alter the checked-in product or the unit tests under review.
"""

import ast
from collections import Counter
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tokenize
import xml.etree.ElementTree as ET

import pytest


REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"

# Recorded from main on 2026-11-05. Counts include every parametrized case.
KNOWN_TESTS = {
    'test_run_checks.py': {
        'test_generated_source_is_rechecked_and_committed_instead_of_old_index': 1,
        'test_second_verification_failure_reaches_executor_retry': 1,
        'test_checks_that_keep_changing_blobs_or_paths_fail_attempt': 2,
        'test_no_changes_are_a_failed_attempt_with_retry_output': 1,
        'test_ci_protocol_alone_is_not_an_implementation_change': 1,
        'test_tracked_protocol_cleanup_is_not_an_implementation_change': 6,
        'test_runner_unit_tests_all_checks_importers_in_either_mode': 2,
        'test_related_units_include_importers_of_deleted_modules_and_existing_changed_tests': 2,
        'test_related_units_exclude_other_modules_tests_after_main_moves': 2,
        'test_related_units_own_only_keeps_changed_tests_and_module_counterparts': 1,
        'test_related_units_own_only_selects_runner_module_test_file': 2,
        'test_pull_request_ci_is_read_from_workflow_files': 8,
        'test_related_unit_failure_uses_implementation_budget_and_reaches_next_prompt': 2,
        'test_related_unit_check_uses_current_budget_excluding_waits': 2,
        'test_related_check_timeout_preserves_implementation_before_cleanup': 2,
        'test_silent_checks_report_progress_and_survive_scheduler_stall_limit': 3,
        'test_check_timeout_uses_retry_limit_and_reports_command': 1,
        'test_retry_gets_remaining_budget_after_checks_spend_time': 1,
        'test_fa_test_keeps_only_the_pytest_launcher': 5,
        'test_fa_test_runs_pytest_in_a_slot_and_returns_its_exit_code': 1,
        'test_check_tells_its_command_that_a_test_slot_is_held': 1,
        'test_fa_test_inside_a_held_slot_runs_without_waiting': 1,
        'test_checks_do_not_write_bytecode_into_the_worktree': 2,
        'test_related_check_timeout_keeps_earlier_done_when_failures': 1,
        'test_protected_allowance_uses_selection_measurement_and_remaining_time': 6,
        'test_protected_timeout_retries_only_with_larger_budget': 4,
    },
    'test_scheduler.py': {
        'test_tick_memory_pressure_leaves_shared_budgets_alone': 2,
        'test_tick_critical_memory_holds_new_starts_and_records_one_row': 2,
        'test_tick_critical_memory_records_nothing_when_files_overlap_already_hold_the_work': 1,
        'test_tick_critical_memory_without_startable_work_records_nothing': 1,
        'test_refresh_populates_blocker_evidence_and_listing_stays_offline': 2,
        'test_refresh_preserves_local_writes_during_remote_reads': 2,
        'test_tick_opens_orchestrator_when_an_earlier_step_raises': 7,
        'test_busy_tick_leaves_the_orchestrator_decision_to_the_lock_holder': 1,
        'test_tick_opens_orchestrator_on_both_github_wait_returns': 1,
        'test_tick_rate_limit_persists_wait_but_other_tracker_errors_raise': 2,
        'test_github_wait_repairs_running_label_without_crash_or_stall_cost': 1,
        'test_outbox_reset_refreshes_before_delivery_inside_the_snapshot_window': 2,
        'test_deleted_issue_outbox_does_not_block_tick_progress': 1,
        'test_github_limit_during_launch_labels_defers_without_creating_a_crash': 1,
        'test_interrupted_runner_with_open_pr_stays_visible': 2,
        'test_human_ready_removal_is_not_promoted_again_for_the_same_approval': 1,
        'test_approval_actor_and_snapshot': 3,
        'test_latest_valid_approval_ignores_other_actors': 1,
        'test_scheduler_projects_follow_ready_running_and_blocked_labels': 1,
        'test_scheduler_moves_unapproved_and_closed_cards_once': 2,
        'test_scheduler_never_adds_cards_for_unknown_closed_or_unapproved_issues': 1,
        'test_scheduler_moves_existing_cards_without_a_local_record': 2,
        'test_scheduler_moves_a_card_people_add_after_the_board_had_none': 1,
        'test_scheduler_repairs_board_after_transport_recovers': 1,
        'test_latest_approver_event_is_the_snapshot_identity': 1,
        'test_removed_approval_and_unauthorized_readdition_do_not_start': 1,
        'test_removed_then_reapproved_takes_new_snapshot': 1,
        'test_body_integrity_for_every_approved_state': 3,
        'test_running_integrity_failure_kills_and_finishes_before_flagging': 2,
        'test_running_overtime_backstop_kills_alive_runner': 2,
        'test_runtime_backstop_outbox_survives_uncollected_old_comment_after_reset': 1,
        'test_tick_removes_registered_worktree_after_runner_termination': 4,
        'test_tick_cleans_worktree_when_real_runner_is_terminated_between_logged_commands': 2,
        'test_failed_termination_keeps_worktree_and_active_record': 1,
        'test_open_review_pr_is_not_rescheduled_or_allowed_to_starve_next_issue': 2,
        'test_pr_opened_between_promotion_and_start_prevents_launch': 1,
        'test_closed_review_pr_allows_issue_to_run_again': 1,
        'test_merged_issue_waiting_for_github_closure_is_not_rescheduled': 6,
        'test_merged_issue_requires_valid_approval_strictly_after_run_start': 6,
        'test_merged_guard_uses_last_run_for_same_issue': 1,
        'test_merge_history_is_rechecked_immediately_before_launch': 1,
        'test_dead_overtime_runner_uses_crash_recovery': 1,
        'test_acceptance_integrity_tracks_origin_changes_and_deletions': 2,
        'test_integrity_details_include_body_and_all_changed_files': 1,
        'test_missing_acceptance_file_is_snapshotted_and_detected_when_added': 1,
        'test_no_machine_check_flags_once_and_human_removal_resumes': 1,
        'test_flagged_issue_is_skipped_entirely': 2,
        'test_blocked_by_waits_until_all_blockers_close': 1,
        'test_one_run_per_project_still_promotes_other_issues': 1,
        'test_lowest_number_starts_and_launcher_pid_is_stored': 1,
        'test_launcher_can_return_no_pid': 1,
        'test_view_opens_once_after_all_available_lanes_launch': 1,
        'test_view_reopen_skips_idle_repository_off_hours_and_no_start': 1,
        'test_view_opener_failure_keeps_all_runs_and_reports_success': 3,
        'test_view_opener_exception_preserves_tick_report_and_next_tick': 2,
        'test_outside_local_run_hours_promotes_without_starting': 1,
        'test_no_start_promotes_without_launching': 1,
        'test_locked_tick_has_no_git_or_tracker_side_effects': 1,
        'test_fetches_configured_base_once_and_holds_tick_lock_through_launch': 1,
        'test_integrity_rechecked_against_fresh_issue_right_before_start': 1,
        'test_acceptance_blobs_rechecked_before_start': 1,
        'test_issue_must_still_be_eligible_before_start': 5,
        'test_start_rechecks_local_eligibility_after_collecting_ready_issues': 8,
        'test_start_keeps_live_issue_when_local_snapshot_row_is_missing': 2,
        'test_flag_only_changes_labels_and_posts_a_reason': 1,
        'test_approver_config_is_case_insensitive': 1,
        'test_first_snapshot_rejects_edits_at_or_after_approval': 3,
        'test_body_changed_and_reverted_before_first_tick_still_requires_reapproval': 1,
        'test_acceptance_changed_after_approval_before_first_tick': 2,
        'test_acceptance_snapshot_uses_approval_commit_and_allows_unrelated_changes': 1,
        'test_missing_approval_time_base_commit_requires_reapproval': 1,
        'test_hash_mismatch_reapproval_is_one_human_action_and_survives_restart': 1,
        'test_reapproval_must_be_strictly_after_flag': 2,
        'test_other_flags_require_human_removal_even_with_new_approval': 3,
        'test_approval_the_hold_already_saw_is_not_recorded_again': 2,
        'test_reapproval_during_a_hold_after_a_body_edit_invalidates_without_release': 1,
        'test_nonapprover_cannot_resume_hash_mismatch': 1,
        'test_approval_event_without_current_label_is_invalid': 2,
        'test_current_label_without_valid_timeline_is_invalid': 1,
        'test_stall_restarts_once_then_flags_second_stall': 1,
        'test_stall_uses_latest_progress_or_start_time': 3,
        'test_failed_stall_termination_does_not_consume_restart': 1,
        'test_stall_intent_survives_runner_finishing_during_termination': 1,
        'test_successful_stall_kill_keeps_count_when_locked_worktree_cleanup_fails': 2,
        'test_dead_active_run_recovers_persisted_stall_intent': 4,
        'test_dead_pid_finishes_run_and_restores_ready': 1,
        'test_alive_or_inaccessible_pid_keeps_project_slot': 2,
        'test_pid_none_has_two_minute_startup_grace': 3,
        'test_two_consecutive_crashes_flag_and_stop_relaunching': 1,
        'test_crash_streak_is_per_issue_and_resets_on_another_outcome': 2,
        'test_dead_run_finishes_without_requeueing_closed_or_flagged_issue': 3,
        'test_cancelled_blocker_flags_once_instead_of_promoting': 1,
        'test_completed_blocker_allows_promotion': 1,
        'test_process_probe_parses_ps_and_matches_exact_issue': 8,
        'test_process_probe_cannot_declare_death_when_ps_fails': 3,
        'test_missing_pid_with_live_runner_keeps_project_slot': 2,
        'test_pid_persistence_failure_does_not_launch_a_second_runner': 1,
        'test_orphan_running_without_record_returns_to_ready': 2,
        'test_orphan_recovery_rechecks_local_labels_before_recording_a_crash': 1,
        'test_recovery_keeps_supplied_issue_when_local_snapshot_row_is_missing': 3,
        'test_crash_restore_rechecks_local_issue_before_changing_labels': 4,
        'test_runner_finishing_during_process_probe_is_not_recovered': 1,
        'test_two_distinct_orphan_running_labels_count_as_consecutive_crashes': 1,
        'test_crash_recovery_replays_after_tracker_failures_without_double_counting': 3,
        'test_ready_issue_reapproved_without_machine_check_is_flagged': 1,
        'test_ready_issue_with_new_or_reopened_blocker_returns_to_approved_waiting': 4,
        'test_ready_issue_with_cancelled_blocker_is_flagged': 1,
        'test_dependencies_are_rechecked_immediately_before_launch': 2,
        'test_machine_check_is_rechecked_from_snapshot_immediately_before_launch': 1,
        'test_custom_fetch_refspec_cannot_hide_changed_acceptance_files': 1,
        'test_new_approval_snapshots_fresh_body_after_stale_issue_list': 2,
        'test_lane_assignment': 4,
        'test_unknown_lane_reports_once_per_tick_and_uses_main_slot': 1,
        'test_lane_single_run_cap_order_and_lowest_issue': 3,
        'test_integrity_is_rechecked_before_each_lane_start': 1,
        'test_paused_lane_skipped_until_expiry': 1,
        'test_tick_runtime_backstop_excludes_waited_seconds': 1,
        'test_tick_does_not_stall_a_waiting_executor': 1,
        'test_limit_wait_can_resume_an_issue_with_an_existing_retry_pr': 1,
        'test_dead_old_format_run_does_not_abort_tick_and_preserves_wip': 2,
        'test_alive_unverified_run_flags_once_and_tick_processes_other_issues': 3,
        'test_refused_tick_stop_preserves_run_and_processes_other_issue': 24,
        'test_refused_stop_after_process_exit_recovers_as_crashed': 3,
        'test_refused_inactive_reaping_continues_tick_and_flags_once': 3,
        'test_crash_cleanup_kills_group_before_removing_worktree': 1,
        'test_crash_group_termination_failure_preserves_worktree_and_active_run': 1,
        'test_dead_run_with_recorded_completion_does_not_signal_group': 4,
        'test_crash_recovery_with_default_killer_tolerates_missing_group': 1,
        'test_overdue_blocker_escalation_is_journaled_once': 1,
        'test_blocker_escalation_rechecks_local_state_before_escalating': 3,
        'test_project_ceiling_counts_only_this_repositorys_runs': 1,
        'test_start_checks_and_github_writes_run_outside_the_machine_lock': 1,
        'test_refresh_tick_reuses_confirmed_reads_and_a_kick_tick_reads_live': 1,
        'test_closed_blocker_releases_a_dependent_whose_own_facts_did_not_change': 1,
        'test_periodic_tick_300_seconds_later_is_a_refresh_tick_despite_fetch_delay': 2,
        'test_tick_without_ready_work_still_applies_machine_parallel_ceiling': 1,
        'test_tick_during_github_wait_still_applies_machine_ceilings_once': 1,
        'test_tick_during_github_wait_still_rechecks_free_usage': 1,
        'test_rate_limit_error_after_budgets_does_not_apply_them_twice': 1,
        'test_tick_memory_hold_history_records_cpu_measurements': 2,
        'test_tick_budget_ceiling_history_records_cpu_measurements': 2,
        'test_tick_during_github_wait_still_expires_the_plan_and_drops_lapsed_pauses': 1,
        'test_refresh_failure_reads_a_new_approval_never_and_a_stored_one_once': 1,
        'test_refresh_failure_with_queued_writes_starts_nothing': 1,
    },
    'test_baseline_cache_unit.py': {
        'test_baseline_caches_all_files_excluded_without_starting_pytest': 4,
        'test_premerge_empty_selection_is_cached_before_reusing_a_broader_baseline': 1,
        'test_baseline_cache_rebuilds_when_pytest_environment_changes': 1,
        'test_baseline_cache_fingerprints_dependency_files': 6,
        'test_baseline_cache_prefers_lockfiles_over_pyproject': 1,
        'test_baseline_cache_fingerprints_python_version': 1,
        'test_baseline_cache_expires_after_six_hours': 1,
        'test_baseline_cache_rebuilds_entries_without_valid_creation_time': 5,
        'test_baseline_clears_pytest_addopts_before_discovery': 3,
        'test_baseline_disables_command_early_stopping': 4,
        'test_baseline_clears_inline_pytest_addopts': 1,
        'test_baseline_does_not_cache_early_stop_from_pytest_plugin': 2,
        'test_baseline_does_not_cache_duplicate_junit_records_after_early_stop': 1,
        'test_baseline_collection_side_effects_do_not_pollute_execution': 1,
        'test_baseline_caches_junit_without_terminal_summary': 2,
        'test_baseline_caches_empty_passing_set_after_complete_execution': 1,
        'test_baseline_cache_preserves_failed_and_skipped_outcomes': 1,
        'test_premerge_cache_reuses_only_covered_outcomes_across_approval_stores': 1,
        'test_runner_reuses_legacy_cache_but_premerge_rebuilds_unknown_outcomes': 1,
        'test_concurrent_prebuild_scheduling_reserves_key_before_launch': 1,
        'test_failed_prebuild_launch_releases_key_for_later_tick': 1,
        'test_queued_prebuild_keeps_original_exclusions_and_sha': 1,
        'test_prebuild_caches_all_files_excluded_without_starting_pytest': 1,
        'test_detached_prebuild_outlives_scheduler_and_retains_reservation': 1,
    },
    'test_run_baseline.py': {
        'test_custom_baseline_check_without_seconds_uses_elapsed_time': 1,
        'test_baseline_preserves_exact_parameterized_ids_and_excludes_nonpasses': 2,
        'test_baseline_cache_rebuilds_collection_errors_even_when_pytest_continues': 1,
        'test_unusable_baseline_stops_before_publish_without_retry': 9,
        'test_baseline_uses_a_prebuilt_cache_before_implementation': 1,
        'test_baseline_budget_exhausted_before_capture_stops_with_unavailable_reason': 1,
        'test_baseline_missing_acceptance_directory_still_allows_run': 1,
        'test_baseline_ignores_only_existing_files_owned_by_approved_unmerged_issues': 1,
        'test_baseline_cache_key_tracks_sorted_excluded_paths_and_caches_zero_tests': 1,
        'test_baseline_failure_exhausts_the_done_when_budget': 1,
        'test_baseline_skip_is_logged_without_creating_a_probe': 2,
        'test_baseline_uses_detection_and_ignores_full_suite_selection_and_generated_files': 1,
        'test_baseline_is_captured_once_and_survives_review_retry': 1,
        'test_empty_baseline_does_not_rerun_failing_tests_or_default_to_the_full_suite': 1,
        'test_baseline_lock_wait_is_credited_and_keeps_the_heartbeat': 1,
        'test_log_dropped_ids_names_one_reason_per_id_in_priority_order': 1,
        'test_log_dropped_ids_in_ci_mode_does_not_claim_a_failure': 1,
        'test_collection_drops_a_parametrized_id_whose_selector_still_selects': 1,
        'test_collection_lists_exact_ids_whatever_the_terminal_settings': 1,
        'test_collection_with_an_import_error_stops_and_drops_nothing': 1,
        'test_collection_does_not_stop_for_an_importorskip_module': 1,
        'test_collection_with_no_tests_or_no_directory_drops_every_id': 1,
        'test_collection_reads_owned_ids_from_the_new_base_exclusions': 1,
        'test_collection_works_with_every_supported_launcher': 4,
        'test_collection_keeps_the_projects_conftest_module': 1,
        'test_collection_runs_even_when_every_id_is_owned': 1,
        'test_collection_that_changes_tracked_files_stops_like_a_capture': 1,
        'test_log_dropped_ids_without_known_failures_does_not_claim_one': 1,
        'test_collection_under_isolated_python_stops_and_drops_nothing': 1,
        'test_collection_plugin_never_collides_with_project_modules': 1,
        'test_with_python_path_joins_inline_and_env_assignments': 1,
        'test_legacy_cache_hit_retains_its_own_duration_without_capture': 2,
    },
    'test_run_execution.py': {
        'test_failed_resume_is_counted_once_even_when_fresh_launch_also_fails': 2,
        'test_executor_nonzero_exit_still_relies_on_independent_checks': 1,
        'test_log_growth_poll_updates_progress_and_quiet_poll_does_not': 1,
        'test_short_executor_records_activity_without_a_periodic_poll': 1,
        'test_claude_native_goal_settings_and_independent_retry': 3,
        'test_claude_deny_paths_are_relative_even_for_absolute_acceptance_dir': 1,
        'test_any_claude_stream_line_updates_progress_through_log_poll': 3,
        'test_transients_preserve_attempt_and_budget_and_fallback_persists': 2,
        'test_assigned_model_survives_transients_instead_of_the_configured_fallback': 1,
        'test_transient_budget_exhaustion_is_not_an_implementation_failure': 1,
        'test_previous_log_errors_do_not_classify_the_current_execution': 1,
        'test_implementer_limit_pauses_lane_and_returns_ready_without_attempt': 1,
        'test_reviewer_availability_waits_then_uses_recovered_verdict': 1,
        'test_reviewer_unreadable_limit_pauses_its_executor_for_configured_wait': 1,
        'test_reviewer_transient_exhaustion_uses_executor_unavailable_flag': 1,
        'test_transient_timeout_result_retries_when_runtime_budget_remains': 1,
        'test_implementation_text_does_not_pause_or_backoff': 3,
        'test_implementer_limit_pauses_its_executor_until_reported_reset': 1,
        'test_reviewer_limit_with_exhausted_usage_waits_for_reset_without_retrying': 1,
        'test_running_issue_does_not_launch_an_executor_paused_by_another_run': 1,
        'test_paused_implementer_is_not_launched_until_its_reset': 1,
        'test_undelivered_feedback_survives_an_executor_error': 1,
        'test_reviewer_limited_during_review_preparation_moves_to_the_worker_once': 1,
        'test_reviewer_limit_written_between_launch_questions_cannot_split_their_answers': 1,
        'test_late_reviewer_limit_with_a_worker_that_cannot_review_waits_instead_of_crashing': 1,
        'test_rerun_after_a_launch_move_decision_does_not_ask_again': 1,
    },
    'test_run_lane_skills.py': {
        'test_scheduler_excludes_injected_lane_skills_from_saved_and_resumed_work': 3,
        'test_lane_executor_model_agent_notes_and_outside_paths': 2,
        'test_lane_null_overrides_and_main_use_project_defaults': 2,
        'test_lane_skills_come_from_worktree_and_only_extend_codex_prompts': 6,
        'test_codex_configured_skills_do_not_require_an_agent_file': 2,
        'test_local_lane_skill_is_available_on_retries_and_never_published': 4,
        'test_lane_skill_list_includes_one_line_description_without_any_body': 4,
        'test_lane_skills_list_every_entry_in_order_with_no_count_limit': 3,
        'test_lane_skills_without_a_description_keep_the_name_and_path': 3,
        'test_missing_lane_skill_is_recorded_once_in_created_or_updated_pr_after_retry': 2,
        'test_lane_skills_do_not_read_a_symlink_outside_the_worktree': 1,
        'test_active_injected_skill_becomes_target_owned_without_leaking_to_wip': 4,
        'test_resumed_injected_skill_becomes_target_owned': 2,
        'test_lane_matched_paths_are_not_reported_outside': 1,
    },
    'test_run_pr_body.py': {
        'test_check_commands_are_escaped_and_failed_checks_are_visible': 1,
        'test_searched_list_is_a_section_only_when_the_lane_wrote_one': 1,
        'test_render_reads_the_latest_searched_file_from_the_worktree': 1,
        'test_a_body_that_fits_is_returned_whole_up_to_the_limit': 2,
        'test_a_body_one_character_over_replaces_only_blocks_past_their_share': 1,
        'test_a_block_length_never_changes_what_another_block_gets': 4,
        'test_every_block_within_its_share_stays_for_any_neighbours': 1,
        'test_required_content_past_the_limit_stops_and_reports_size_and_limit': 1,
        'test_omission_notes_count_before_anything_can_be_sent': 1,
        'test_required_values_with_markdown_look_stay_exact': 1,
        'test_comments_hold_the_whole_text_in_named_chunks_and_are_posted_once': 1,
        'test_changed_text_gets_its_own_comments': 1,
        'test_a_failed_comment_is_reported_and_a_rate_limit_still_waits': 1,
        'test_no_usable_summary_means_the_issue_title_and_the_log_says_so': 6,
        'test_a_written_summary_is_used_as_it_is': 1,
        'test_the_record_holds_rendering_inputs_and_no_finished_body': 1,
        'test_refresh_without_a_record_stops_before_any_write': 1,
        'test_a_review_refresh_posts_its_comments_before_the_review_reads_the_pr': 1,
        'test_a_local_re_review_records_its_verdict_for_the_next_composition': 1,
        'test_merge_retitle_has_no_body_without_a_record': 3,
        'test_retitle_without_a_record_changes_the_title_and_writes_no_body': 1,
        'test_a_failed_title_change_without_a_record_merges_with_the_subject': 1,
        'test_no_record_merge_sends_the_issue_title_as_subject_and_no_body_at_gh': 1,
        'test_retitle_with_a_record_sends_the_composition_and_its_comments': 1,
        'test_retitle_with_the_right_title_composes_nothing': 1,
        'test_merge_retitle_composes_from_the_record_and_never_from_the_old_body': 1,
        'test_merge_retitle_writes_nothing_when_its_record_cannot_be_composed': 1,
        'test_merge_retitle_body_of_a_batch_comes_from_the_tracker': 1,
        'test_an_update_after_a_review_also_posts_its_comments': 1,
        'test_a_held_harness_publication_reports_a_body_it_cannot_compose': 1,
        'test_a_held_harness_publication_posts_the_comments_its_notes_name': 1,
        'test_an_early_native_publication_carries_the_try_text_as_it_is': 1,
        'test_fixed_format_writers_stay_structured_values': 1,
        'test_the_prompt_asks_for_the_summary_as_a_value': 1,
    },
    'test_run_review.py': {
        'test_review_pass_opens_pr_and_records_review': 2,
        'test_review_prompt_excludes_main_only_changes': 3,
        'test_review_failure_retries_implementation_and_done_when': 1,
        'test_exceeding_review_max_opens_pr_with_risk_high_hold': 2,
        'test_review_failure_uses_shared_retry_budget': 1,
        'test_review_dispute_requires_consecutive_findings_after_rebuttal': 4,
        'test_unstructured_reviews_with_shared_preamble_do_not_create_a_dispute': 1,
        'test_repeated_unstructured_finding_normalizes_wrapping_after_rebuttal': 1,
        'test_rebuttal_history_reaches_pr_and_codex_review_even_when_executor_stages_it': 1,
        'test_reviewer_edits_are_discarded_before_commit': 1,
        'test_disabled_review_does_not_create_a_reviewer': 1,
        'test_unusable_review_retries_implementation_using_the_shared_budget': 21,
        'test_full_claude_verdict_survives_output_tail_truncation': 1,
        'test_reviewer_timeout_at_runtime_limit_does_not_launch_more_sessions': 1,
        'test_reviewer_commit_is_detected_and_head_index_and_files_are_restored': 1,
        'test_latin1_source_reaches_review_and_pr_without_corruption': 1,
        'test_code_review_result_parsing': 15,
        'test_none_marker_reads_the_same_on_the_first_or_last_line': 2,
        'test_code_review_plain_finding_reports_failure': 1,
        'test_code_review_numbered_backtick_finding_reports_failure': 1,
        'test_code_review_invalid_json_or_error_is_unusable': 6,
        'test_code_review_accepts_reported_json_array_shape': 18,
        'test_code_review_multiple_json_arrays_are_unusable': 4,
        'test_code_review_arrays_outside_findings_payload_are_unusable': 12,
        'test_code_review_partial_findings_fail': 5,
        'test_code_review_bad_arrays_are_unusable_without_passing_on_nested_empty_arrays': 14,
        'test_code_review_json_array_reports_every_item': 1,
        'test_pr_body_reviews_published_head_and_keeps_findings_in_comments': 4,
        'test_review_of_legacy_head_preserves_tracked_exclusions': 2,
        'test_claude_review_scope_restores_original_or_removes_temporary_file': 21,
        'test_claude_review_scope_carries_authored_content_without_gate_writing': 1,
        'test_claude_review_scope_does_not_accumulate_between_reviews': 1,
        'test_claude_review_scope_preserves_a_local_instructions_symlink': 1,
        'test_codex_review_gets_the_same_rules_without_a_temporary_scope_file': 1,
        'test_native_json_findings_trigger_prompt_confirmation_and_save_raw_results': 2,
        'test_native_prose_empty_array_cannot_skip_defects_and_merge': 2,
        'test_unusable_review_saves_full_result_and_hands_off_full_raw_text': 6,
        'test_unusable_review_followed_by_fail_keeps_both_raw_results': 1,
        'test_invalid_native_envelope_is_saved_and_handed_off_unchanged': 1,
        'test_native_unusable_review_retries_implementation_then_passes_or_exhausts_budget': 8,
        'test_native_review_findings_repair_and_reviewer_commits_are_restored': 1,
        'test_review_returns_conflicted_pr_to_implementer_even_with_pass_verdict': 2,
        'test_review_text_about_limits_uses_normal_review_failure_budget': 1,
        'test_pr_head_verification_reports_every_failing_command': 1,
        'test_pr_head_timeout_saves_earlier_failures_and_the_timed_out_output': 1,
        'test_pr_head_exception_keeps_the_failures_gathered_before_it': 1,
        'test_pr_head_launch_error_keeps_the_previously_saved_failures': 1,
        'test_lane_decisions_block_is_appended_replaced_and_dropped_once': 1,
        'test_pr_body_review_receives_the_lanes_decide_lines': 1,
        'test_pr_body_re_review_refreshes_the_lanes_decide_lines': 1,
        'test_review_requires_exactly_one_verdict': 8,
        'test_missing_verdict_keeps_protocol_separate_from_empty_findings': 32,
        'test_empty_review_merges_without_rework_or_a_recorded_pass': 4,
        'test_failed_review_retains_retry_ceiling_without_a_defect_hold': 2,
        'test_confirmation_failure_retains_retry_ceiling_without_a_defect_hold': 4,
        'test_break_it_confirmation_cannot_lose_the_execution_ceiling': 2,
        'test_break_it_rejects_charging_unusable_reviews_as_defects': 1,
        'test_break_it_rejects_a_native_scope_without_the_verdict_contract': 1,
        'test_break_it_rejects_a_native_scope_without_the_mechanism_map': 1,
        'test_break_it_rejects_native_none_becoming_a_pass': 1,
        'test_break_it_native_confirmation_rejects_a_false_first_pass': 5,
        'test_break_it_shared_retry_budget_rejects_an_extra_unusable_rework': 1,
    },
    'test_run_previous.py': {
        'test_retry_marker_adds_default_before_hint_and_is_consumed_with_it': 1,
        'test_producers_only_append_and_delivery_removes_only_what_the_prompt_held': 1,
        'test_stop_point_prefers_final_message_then_this_turns_log_tail': 1,
        'test_rebuild_reason_needs_more_than_half_of_own_files': 1,
        'test_path_helper_keeps_runs_issue_directory_for_reviews': 1,
        'test_review_class_question_follows_repository_language_in_pr_body_rules': 1,
        'test_stop_point_is_recorded_when_a_usage_limit_ends_the_turn': 1,
        'test_rebuild_judges_conflicts_still_unresolved_after_partial_resolution': 1,
        'test_rerun_review_adds_the_class_question_to_an_older_pr_scope': 1,
    },
    'test_ci_mode_unit.py': {
        'test_ci_protection_is_independent_of_evidence_target': 4,
        'test_unfinished_measurement_polls_within_existing_ci_budget': 6,
        'test_report_on_a_later_base_is_read_on_the_base_ci_measured': 2,
        'test_older_base_callback_only_refreshes_valid_stale_base': 6,
        'test_unknown_reported_base_waits_for_evidence_without_refresh': 4,
        'test_individual_ci_report_errors_are_unusable_during_import_and_locked_read': 8,
        'test_report_serialization_errors_are_unusable': 3,
        'test_numeric_and_nested_report_faults_are_unusable_comparisons': 2,
        'test_compare_records_parallel_wall_duration_and_detects_deselection': 1,
        'test_rejects_unusable_shards': 7,
        'test_only_freshness_mismatches_are_stale': 2,
        'test_suite_and_listing_rejections_name_field_observed_and_expected': 10,
        'test_excluded_test_in_report_names_the_test_and_exclusions': 1,
        'test_shards_must_record_the_same_exclusions': 1,
        'test_config_rejects_invalid_ci_settings': 9,
        'test_workflow_runner_visibility_warning_and_rewrite': 3,
        'test_checked_in_acceptance_workflow_matches_generator': 1,
        'test_workflow_removes_only_generated_legacy_publisher': 2,
        'test_measurement_partitions_all_portable_nodes_and_excludes_marked_and_locked_tests': 1,
        'test_force_added_exclusions_survive_generic_artifact_cleanup': 1,
        'test_runner_adds_no_empty_commit_when_exclusions_match': 2,
        'test_runner_adds_exactly_one_empty_commit_when_exclusions_change': 3,
        'test_runner_carries_exclusions_on_the_content_commit': 1,
        'test_runner_carries_current_exclusions_on_its_base_merge': 1,
        'test_shard_rejects_invalid_or_duplicate_exclusion_trailers': 14,
        'test_fork_check_results_are_unusable_without_fetching_checks': 1,
        'test_local_only_base_failures_are_ignored_but_head_unit_failures_are_not': 2,
        'test_local_only_unit_check_requires_selected_tests': 12,
        'test_tracker_reads_every_page_of_head_check_runs': 1,
        'test_local_mode_generated_workflow_does_not_require_ci_inputs': 1,
        'test_shard_without_head_exclusions_succeeds_without_a_report': 1,
        'test_shard_does_not_inherit_a_trailer_from_the_base_or_read_a_stale_file': 2,
        'test_ci_head_without_exclusions_and_reports_holds_or_falls_back': 2,
        'test_large_comparisons_fit_github_output_without_losing_any_ids': 1,
        'test_damaged_compressed_report_is_unusable': 1,
        'test_shard_publishes_real_base_and_head_measurements': 2,
        'test_shard_reads_newest_trailer_below_untrailed_merge_and_ignores_stale_tree': 2,
        'test_shard_rejects_measurement_that_rewrites_tracked_source': 4,
        'test_local_fallback_cannot_merge_the_head_before_a_tracked_rewrite': 2,
        'test_local_baseline_rejects_measurement_that_rewrites_tracked_source': 4,
        'test_review_checks_unit_selection_honors_runner_unit_tests': 3,
        'test_workflow_only_measures_issue_group_and_batch_merge_prs_to_the_primary_base': 16,
        'test_unrelated_producers_cannot_supply_acceptance_evidence': 10,
        'test_measurement_cannot_read_publication_credentials': 1,
        'test_head_configuration_cannot_erase_baseline_scope': 1,
        'test_report_cannot_shrink_the_expected_four_shard_layout': 1,
        'test_non_pytest_ci_uses_local_comparison_and_invokes_worker': 2,
        'test_publisher_accepts_only_complete_artifacts_from_the_triggering_run': 8,
        'test_base_local_or_non_pytest_configuration_skips_ci_measurement': 2,
        'test_changed_acceptance_workflow_requires_local_comparison_even_with_fabricated_shards': 4,
        'test_tracker_binds_borrowed_publisher_url_to_github_suite_and_run_ids': 4,
        'test_local_only_baseline_rejects_exit_after_preexisting_failure_before_remaining_test': 1,
        'test_local_mode_runs_machine_only_importer_when_ci_owns_portable_units': 1,
        'test_undetected_test_command_uses_local_path_and_invokes_worker': 1,
        'test_final_lock_rechecks_comparison_and_releases_before_reverification': 18,
        'test_tracker_rejects_damaged_or_ambiguous_artifacts': 8,
        'test_workflow_equality_and_pr_changes_are_distinct': 8,
        'test_lagging_target_cannot_trust_artifacts_from_an_old_workflow': 2,
        'test_trusted_workflow_revision_uses_repository_default_branch': 1,
        'test_missing_trusted_default_branch_obeys_fallback_configuration': 2,
        'test_github_binary_download_preserves_bytes_and_text_errors': 1,
        'test_acceptance_closes_only_an_open_evidence_refusal': 1,
        'test_refusal_reason_comes_from_the_refused_branch': 4,
        'test_failed_observation_never_replaces_the_comparison_result': 1,
        'test_each_changed_refusal_at_a_read_keeps_its_own_cause_and_acceptance_closes': 1,
        'test_a_moved_read_names_what_moved_without_reading_reports': 3,
        'test_trusted_branch_and_scope_changes_are_named': 1,
        'test_a_rate_limit_at_a_read_stays_the_callers_and_is_no_refusal': 1,
    },
}


def private(name):
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def private_foragent_name(name):
    parts = name.split(".")
    return parts[0] == "foragent" and any(private(part) for part in parts[1:])


def private_accesses(source):
    """Check syntax, including aliases, local imports and explicit patch targets."""
    tree = ast.parse(source)
    bindings = {}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "foragent":
            if private_foragent_name(node.module):
                found.append((node.lineno, f"from {node.module} import ..."))
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
                if private(alias.name) or alias.name == "*":
                    found.append((node.lineno, f"from {node.module} import {alias.name}"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "foragent":
                    bindings[alias.asname or "foragent"] = alias.name if alias.asname else "foragent"
                    if private_foragent_name(alias.name):
                        found.append((node.lineno, f"import {alias.name}"))

    def imported_name(node):
        if isinstance(node, ast.Name):
            return bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = imported_name(node.value)
            return f"{parent}.{node.attr}" if parent else None
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name = imported_name(node)
            if name and private(node.attr):
                found.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Call):
            call = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            args = node.args
            if call in {"import_module", "__import__", "importorskip", "setattr", "getattr", "delattr", "patch", "object"}:
                if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                    if private_foragent_name(args[0].value):
                        found.append((node.lineno, ast.unparse(node)))
                if (call in {"setattr", "getattr", "delattr", "object"} and len(args) > 1
                        and imported_name(args[0]) and isinstance(args[1], ast.Constant)
                        and isinstance(args[1].value, str) and private(args[1].value)):
                    found.append((node.lineno, ast.unparse(node)))
    return sorted(set(found))


def imported_helpers(source):
    """Follow test helpers so relocating a private import does not satisfy cleanup."""
    helpers = set()
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        for name in names:
            # Other test modules (e.g. test_runner) have their own cleanup issues.
            if name.split(".")[0] not in {"helpers", "runner_support"}:
                continue
            base = TESTS.joinpath(*name.split("."))
            helpers.update(candidate for candidate in (base.with_suffix(".py"), base / "__init__.py")
                           if candidate.is_file())
    return helpers


def file_findings(path):
    findings, seen, pending = [], set(), [path]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        source = current.read_text(encoding="utf-8")
        findings.extend(f"{current.relative_to(REPO)}:{line}: {description}"
                        for line, description in private_accesses(source))
        pending.extend(imported_helpers(source) - seen)
    return findings


def test_public_name_check_follows_transitive_helpers(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "TESTS", tmp_path / "tests")
    helpers = TESTS / "helpers"
    helpers.mkdir(parents=True)
    unit = TESTS / "test_example.py"
    unit.write_text("from helpers import first\n", encoding="utf-8")
    (helpers / "first.py").write_text("import helpers.second\n", encoding="utf-8")
    (helpers / "second.py").write_text(
        "from foragent.runner import _check\nfrom helpers import first\n", encoding="utf-8",
    )
    assert file_findings(unit) == ["tests/helpers/second.py:1: from foragent.runner import _check"]


@pytest.mark.parametrize("source", [
    "from foragent.runner import _check",
    "from foragent.runner import _check as check",
    "def test_local():\n    from foragent.run_execution import _available_executor",
    "from foragent._build import GIT_SHA",
    "import foragent.commands._pm_table as table",
    "from foragent import runner as subject\nsubject._check()",
    "import foragent.runner\nforagent.runner._check()",
    "importlib.import_module('foragent.commands._pm_table')",
    "__import__('foragent.commands._pm_table')",
    "pytest.importorskip('foragent.commands._pm_table')",
    "patch('foragent.runner._check')",
    "from foragent import runner\nmonkeypatch.setattr(runner, '_check', fake)",
    "from foragent import runner\ngetattr(runner, '_check')",
    "from foragent import runner\npatch.object(runner, '_check', fake)",
    "from foragent.runner import *",
])
def test_public_name_check_detects_private_imports_and_accesses(source):
    assert len(private_accesses(source)) == 1


def test_public_name_check_allows_public_apis_and_test_local_helpers():
    source = (
        "from foragent import runner\n"
        "from foragent.runner import run_issue\n"
        "from foragent import __version__\n"
        "from runner_support import _helper\n"
        "def _local(): pass\n"
        "runner.run_issue(1, ctx)\n"
        "monkeypatch.setattr(runner, 'run_logged', fake)\n"
        "monkeypatch.setattr(runner.os, 'getpid', fake)\n"
        "fake._local()\n"
        "importlib.import_module('foragent.runner')\n"
    )
    assert private_accesses(source) == []


@pytest.mark.parametrize("name", sorted(KNOWN_TESTS))
def test_run_unit_file_uses_only_public_foragent_names(name):
    path = TESTS / name
    assert path.is_file(), f"tests/{name} was removed"
    found = file_findings(path)
    assert found == [], "Private Foragent dependencies remain:\n" + "\n".join(found)


@pytest.mark.parametrize("name", sorted(KNOWN_TESTS))
def test_run_unit_file_keeps_every_original_test_name(name):
    path = TESTS / name
    assert path.is_file(), f"tests/{name} was removed"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    present = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert set(KNOWN_TESTS[name]) <= present, f"tests/{name} lost tests: {set(KNOWN_TESTS[name]) - present}"


def environment(source):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env["PYTHONPATH"] = os.pathsep.join([str(source), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])])
    return env


def run_units(directory, targets, source=REPO / "src"):
    report = directory / "results.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "-p", "no:xdist",
         "-p", "no:cacheprovider", "-p", "no:randomly", f"--junitxml={report}", *targets],
        cwd=REPO, env=environment(source), capture_output=True, text=True, timeout=1800,
    )
    assert report.is_file(), result.stdout[-5000:] + result.stderr[-2000:]
    cases = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        key = (case.get("classname", "").rsplit(".", 1)[-1] + ".py", case.get("name", ""))
        cases.setdefault(key, []).append(case)
    assert cases, result.stdout[-5000:] + result.stderr[-2000:]
    return result, cases


def problems(case):
    return [child for child in case if child.tag in {"failure", "error", "skipped"}]


@pytest.fixture(scope="module")
def intact_units(tmp_path_factory):
    """Run each unit file once, on first use, so one Done when command can check a few files
    within the runner's 15 min command limit (2026-11-06, #2176 spec-question)."""
    runs = {}

    def run(name):
        if name not in runs:
            runs[name] = run_units(tmp_path_factory.mktemp("run-public-intact"), [f"tests/{name}"])
        return runs[name]
    return run


# Machine-only (CI runs these unit files itself; whole-file subprocess runs here exceed the 30 min CI job, 2026-11-06).
@pytest.mark.local_only
@pytest.mark.parametrize("name", sorted(KNOWN_TESTS))
def test_run_unit_file_keeps_all_original_cases_passing(intact_units, name):
    result, cases = intact_units(name)
    counts = Counter()
    failing = []
    for (filename, node), entries in cases.items():
        if filename != name:
            continue
        base = node.split("[", 1)[0]
        counts[base] += len(entries)
        failing.extend((node, child.tag, child.get("message", ""))
                       for entry in entries for child in problems(entry))
    actual = {test: counts[test] for test in KNOWN_TESTS[name]}
    assert actual == KNOWN_TESTS[name], f"tests/{name} changed execution counts: {actual}"
    assert failing == [], (failing, result.stdout[-5000:] + result.stderr[-2000:])
    assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-2000:]


# (case id, existing unit file, existing test, product file, old text, broken text)
BREAKS = [
    ("check-writes-bytecode", "test_run_checks.py", "test_checks_do_not_write_bytecode_into_the_worktree",
     "run_checks.py", '"PYTHONDONTWRITEBYTECODE": "1"}',
     '"PYTHONDONTWRITEBYTECODE": ""}'),
    ("scheduler-skips-launch", "test_scheduler.py", "test_lowest_number_starts_and_launcher_pid_is_stored",
     "scheduler_start.py", "pid = ctx.launcher(number)", "pid = None"),
    ("scheduler-launches-twice", "test_scheduler.py", "test_lowest_number_starts_and_launcher_pid_is_stored",
     "scheduler_start.py", "pid = ctx.launcher(number)", "pid = ctx.launcher(number)\n        pid = ctx.launcher(number)"),
    ("scheduler-stores-wrong-pid", "test_scheduler.py", "test_lowest_number_starts_and_launcher_pid_is_stored",
     "scheduler_start.py", "pid = ctx.launcher(number)", "pid = ctx.launcher(number) + 1"),
    ("cache-captures-twice", "test_baseline_cache_unit.py", "test_baseline_collection_side_effects_do_not_pollute_execution",
     "run_baseline.py", "saved = load_reusable(cache, key)", "saved = None"),
    ("baseline-loses-duration", "test_run_baseline.py", "test_custom_baseline_check_without_seconds_uses_elapsed_time",
     "run_baseline.py", "duration_seconds = monotonic() - started if result.seconds is None else result.seconds",
     "duration_seconds = 0.0 if result.seconds is None else result.seconds"),
    ("execution-loses-activity", "test_run_execution.py", "test_short_executor_records_activity_without_a_periodic_poll",
     "run_execution.py", "ctx.store.update_run(n, last_progress_at=timestamp, last_activity_at=timestamp)",
     "ctx.store.update_run(n, last_progress_at=timestamp)"),
    ("lane-skills-listed-twice", "test_run_lane_skills.py", "test_lane_skills_list_every_entry_in_order_with_no_count_limit",
     "run_lane_skills.py", 'sections.append(f"- {name}: {description} (.claude/skills/{name}/SKILL.md)")',
     'sections.extend([f"- {name}: {description} (.claude/skills/{name}/SKILL.md)"] * 2)'),
    ("pr-skips-comments", "test_run_pr_body.py", "test_comments_hold_the_whole_text_in_named_chunks_and_are_posted_once",
     "run_pr_body.py", 'ctx.tracker.comment(pr, f"{header}\\n{text[start:start + COMMENT_CHUNK]}")', "pass"),
    ("pr-duplicates-comments", "test_run_pr_body.py", "test_comments_hold_the_whole_text_in_named_chunks_and_are_posted_once",
     "run_pr_body.py", 'ctx.tracker.comment(pr, f"{header}\\n{text[start:start + COMMENT_CHUNK]}")',
     'ctx.tracker.comment(pr, f"{header}\\n{text[start:start + COMMENT_CHUNK]}")\n'
     '                    ctx.tracker.comment(pr, f"{header}\\n{text[start:start + COMMENT_CHUNK]}")'),
    ("pr-changes-comment-header", "test_run_pr_body.py", "test_comments_hold_the_whole_text_in_named_chunks_and_are_posted_once",
     "run_pr_body.py", 'ctx.tracker.comment(pr, f"{header}\\n{text[start:start + COMMENT_CHUNK]}")',
     'ctx.tracker.comment(pr, f"wrong {header}\\n{text[start:start + COMMENT_CHUNK]}")'),
    ("review-accepts-two-verdicts", "test_run_review.py", "test_review_requires_exactly_one_verdict",
     "run_review.py", "if len(verdict_lines) != 1 or verdict_lines[0][1] is None:",
     "if not verdict_lines or verdict_lines[0][1] is None:"),
    ("previous-skips-hint", "test_run_previous.py", "test_producers_only_append_and_delivery_removes_only_what_the_prompt_held",
     "run_previous.py", 'store._atomic_write(path, f"{unused.rstrip()}\\n\\n{message}" if unused.strip() else message)', "pass"),
    ("previous-duplicates-hint", "test_run_previous.py", "test_producers_only_append_and_delivery_removes_only_what_the_prompt_held",
     "run_previous.py", 'store._atomic_write(path, f"{unused.rstrip()}\\n\\n{message}" if unused.strip() else message)',
     'store._atomic_write(path, (f"{unused.rstrip()}\\n\\n{message}" if unused.strip() else message) * 2)'),
    ("previous-changes-hint", "test_run_previous.py", "test_producers_only_append_and_delivery_removes_only_what_the_prompt_held",
     "run_previous.py", 'store._atomic_write(path, f"{unused.rstrip()}\\n\\n{message}" if unused.strip() else message)',
     'store._atomic_write(path, (f"{unused.rstrip()}\\n\\n{message}" if unused.strip() else message) + "!")'),
    ("ci-trusts-wrong-branch", "test_ci_mode_unit.py", "test_trusted_workflow_revision_uses_repository_default_branch",
     "ci_mode.py", 'return git.ref_sha(f"origin/{tracker.pr_default_branch()}")',
     'return git.ref_sha("origin/main")'),
]


def test_break_it_cases_cover_every_target_unit_file_once_or_more():
    assert {name for _, name, *_ in BREAKS} == set(KNOWN_TESTS)
    assert len({case for case, *_ in BREAKS}) == len(BREAKS)


def mutation_anchor_spans(source, snippet):
    """Find token-equivalent anchors, retaining offsets in the original source."""
    ignored = {tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
               tokenize.COMMENT, tokenize.ENDMARKER}

    def tokens(text, *, fragment=False):
        found = []
        try:
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type in ignored:
                    continue
                value = token.string
                if token.type == tokenize.STRING:
                    value = ast.literal_eval(value)
                elif token.type == tokenize.FSTRING_START:
                    value = value.rstrip("'\"")
                elif token.type == tokenize.FSTRING_END:
                    value = ""
                found.append((token.type, value, token.start, token.end))
        except tokenize.TokenError as error:
            # A snippet may start or end inside an expression, e.g. a dict entry.
            if not fragment or error.args[0] != "EOF in multi-line statement":
                raise
        return found

    actual = tokens(source)
    expected = [token[:2] for token in tokens(snippet, fragment=True)]
    if not expected:
        return []
    offsets = [0]
    for line in io.StringIO(source):
        offsets.append(offsets[-1] + len(line))
    spans = []
    for index in range(len(actual) - len(expected) + 1):
        window = actual[index:index + len(expected)]
        if [token[:2] for token in window] == expected:
            start, end = window[0][2], window[-1][3]
            spans.append((offsets[start[0] - 1] + start[1], offsets[end[0] - 1] + end[1]))
    return spans


def test_mutation_anchor_matcher_ignores_formatting_and_quotes():
    anchor = "emit(\n        'hello',  # same argument\n        count = 2\n    )"
    source = f"def example():\n    {anchor}\n    keep()\n"
    snippet = 'emit("hello", count=2)'
    start = source.index("emit")
    assert mutation_anchor_spans(source, snippet) == [(start, start + len(anchor))]


@pytest.mark.local_only
@pytest.mark.parametrize("case,name,test,product,old,new", BREAKS, ids=[entry[0] for entry in BREAKS])
def test_breaking_checked_behaviour_still_fails_the_original_test(
    tmp_path, case, name, test, product, old, new,
):
    intact_result, intact = run_units(tmp_path / "intact", [f"tests/{name}::{test}"])
    expected = {key: entries for key, entries in intact.items()
                if key[0] == name and key[1].split("[", 1)[0] == test}
    assert sum(len(entries) for entries in expected.values()) == KNOWN_TESTS[name][test]
    assert all(not problems(entry) for entries in expected.values() for entry in entries), intact_result.stdout[-5000:]

    source = tmp_path / "src"
    shutil.copytree(REPO / "src", source, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    target = source / "foragent" / product
    original = target.read_text(encoding="utf-8")
    matches = mutation_anchor_spans(original, old)
    assert matches, f"{case}: anchor moved in {product}; BREAKS needs the new snippet"
    assert len(matches) == 1, f"{case}: mutation anchor must occur exactly once in {product}"
    start, end = matches[0]
    changed = original[:start] + new + original[end:]
    compile(changed, str(target), "exec")
    target.write_text(changed, encoding="utf-8")
    probe = subprocess.run(
        [sys.executable, "-c", "import foragent; print(foragent.__file__)"],
        cwd=REPO, env=environment(source), capture_output=True, text=True, timeout=30,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert Path(probe.stdout.strip()).resolve().is_relative_to(source.resolve()), probe.stdout

    result, broken = run_units(tmp_path, [f"tests/{name}::{test}"], source)
    assert set(broken) == set(expected), (case, broken.keys(), result.stdout[-5000:])
    assert result.returncode == 1, (case, result.stdout[-5000:] + result.stderr[-2000:])
    for key, entries in broken.items():
        assert len(entries) == len(expected[key]) == 1, (case, key, "test ran more than once")
        failures = problems(entries[0])
        assert len(failures) == 1 and failures[0].tag == "failure", (case, key, result.stdout[-5000:])
        # Pytest's JUnit report records the exception in the traceback, without
        # a "type" attribute on <failure>.
        assert (failures[0].text or "").rstrip().endswith(": AssertionError"), (
            case, key, result.stdout[-5000:],
        )
