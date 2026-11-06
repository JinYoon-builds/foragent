"""No unit test file or CI unit shard carries most of the merge-owner batch tests.

tests/test_merge_owner_batch.py alone took 706 s of CI time on main (run
41935136743, commit 16c4e500), so its shard ran past the gate's CI wait and
green PRs were held as "CI did not finish". The batch tests move by concern
into several unit test files: every test keeps its name, its parametrized
cases and its assertions, and no single file or CI unit shard holds more than
two fifths of the recorded batch time.

KNOWN maps each batch test on origin/main to (CI seconds in that run,
collected cases, assert statements). If main adds a batch test before this
issue lands, the lane may add it here with its measured CI seconds; it may
not drop or rename an entry.
"""

import ast
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from foragent.ci_unit_shard import select, unit_files

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
TESTS = ROOT / "tests"
ACCEPTANCE = TESTS / "acceptance"
SHARE = 0.40  # one file or shard may hold at most this share of the recorded batch time / cases

KNOWN = {  # test name: (CI seconds on main, collected cases, assert statements)
    "test_red_train_retries_ordered_half_then_whole_remainder_and_halves_again": (16.0, 1, 11),
    "test_red_singleton_is_spent_and_returns_to_the_existing_single_path": (7.6, 1, 7),
    "test_timed_out_retry_releases_unrelated_batch_on_next_tick": (8.4, 1, 7),
    "test_non_green_retry_retirement_resolves_failed_set_across_bases": (16.6, 2, 8),
    "test_uncomposable_failed_half_falls_through_to_normal_batching": (6.8, 1, 6),
    "test_retired_composed_half_falls_through_to_normal_batching": (7.7, 1, 3),
    "test_failed_half_uses_join_exclusions_without_filling_from_outsiders": (11.0, 1, 6),
    "test_resolved_failed_set_does_not_constrain_normal_composition": (26.0, 2, 3),
    "test_only_a_later_green_full_set_on_moved_main_resolves_a_failure": (34.2, 4, 4),
    "test_newest_failure_wins_over_git_ref_sort_order": (9.7, 1, 2),
    "test_only_tip_published_and_reused_by_single_path_across_restart": (24.5, 3, 12),
    "test_green_tip_lands_members_whose_own_ci_is_pending": (4.7, 1, 2),
    "test_member_already_in_prefix_retires_batch_without_excluding_its_checks": (4.2, 1, 9),
    "test_workflow_changing_member_uses_single_fallback_while_remaining_members_batch": (16.8, 2, 7),
    "test_nonacceptance_workflow_change_remains_a_batch_member": (4.7, 1, 2),
    "test_main_only_workflow_change_keeps_old_members_in_batch": (9.7, 2, 4),
    "test_workflow_comparison_failure_falls_back_to_single_path": (3.3, 1, 3),
    "test_remote_only_candidate_does_not_block_locally_available_batch": (4.6, 1, 5),
    "test_pending_tip_does_not_publish_other_prefixes": (8.7, 1, 6),
    "test_batch_size_limits_prefixes_and_survives_restart": (26.2, 2, 2),
    "test_ci_green_cannot_bypass_any_fresh_locked_single_path_check": (66.3, 11, 6),
    "test_red_tip_retires_whole_train_without_landing": (8.9, 2, 3),
    "test_rejected_target_push_retires_train_and_members_resume_single_path": (12.8, 2, 4),
    "test_target_moving_under_lock_is_rejected_even_when_its_tree_is_identical": (4.2, 1, 3),
    "test_restart_after_atomic_push_records_every_member_from_ancestry": (12.3, 2, 4),
    "test_github_lag_after_successful_push_reconciles_without_duplicate_landing": (5.0, 1, 5),
    "test_completion_io_failure_replays_all_members_after_atomic_push": (4.6, 1, 2),
    "test_cleanup_accepts_tip_pr_already_marked_merged_by_github": (4.5, 1, 2),
    "test_cleanup_restart_keeps_the_retired_tip_and_never_reuses_evidence": (4.7, 1, 5),
    "test_retirement_reason_is_durable_logged_and_preserved_on_replay": (0.5, 2, 9),
    "test_disabling_batching_retires_published_prefixes_idempotently": (5.5, 1, 2),
    "test_external_retirement_does_not_spend_member_generations": (14.4, 4, 2),
    "test_single_path_final_failure_spends_only_the_failing_member": (5.6, 1, 3),
    "test_target_workflow_change_retires_train_without_spending_members": (4.2, 1, 5),
    "test_reservation_also_guards_direct_single_path_advancement": (4.3, 1, 4),
    "test_unusable_candidate_stops_evidence_and_members_resume_single_path": (22.8, 4, 4),
    "test_restart_recreates_missing_prefix_prs_from_refs_without_republishing": (9.4, 2, 4),
    "test_main_conflict_returns_member_to_lane_and_fills_batch_from_later_candidates": (5.4, 1, 13),
    "test_conflict_with_earlier_member_skips_only_that_member": (5.2, 1, 7),
    "test_all_main_conflicts_return_to_lanes_in_one_pass_and_only_once": (12.9, 2, 10),
    "test_single_main_conflict_returns_to_lane_without_a_batch": (1.8, 1, 7),
    "test_workflow_exclusion_does_not_send_a_main_conflict_to_lane": (4.6, 1, 5),
    "test_unavailable_base_conflict_probe_keeps_admission_for_single_path": (4.1, 1, 6),
    "test_main_conflict_rechecks_pr_head_before_returning_to_lane": (4.2, 1, 6),
    "test_main_conflict_repair_resumes_after_lost_close_response": (5.8, 1, 9),
    "test_main_conflict_repair_rate_limit_stops_batch_publication_until_resumed": (5.9, 1, 12),
    "test_main_conflict_partial_repair_does_not_report_return_to_lane": (11.5, 2, 8),
    "test_all_but_one_conflicting_gives_no_batch_or_retirement_on_retry": (4.9, 1, 5),
    "test_member_composition_failure_leaves_single_path_available": (8.3, 1, 6),
    "test_retired_identity_uses_only_composed_members": (6.3, 1, 8),
    "test_merge_batch_false_uses_the_unchanged_single_path": (5.0, 1, 2),
    "test_merge_batch_requires_owner_ci_and_a_boolean": (0.0, 3, 0),
    "test_retirement_cancels_owned_prefix_ci_before_deleting_refs_and_retries": (4.1, 1, 9),
    "test_retargeted_prefix_is_retained_by_the_owner_close_guard": (5.8, 1, 7),
    "test_train_related_units_use_published_tip_receipt_and_keep_locked_commands": (5.2, 1, 7),
    "test_train_ci_includes_every_members_done_when_and_excludes_only_outsiders": (3.7, 1, 6),
    "test_train_validates_later_members_acceptance_locks_on_composed_tip": (4.9, 1, 8),
    "test_train_retires_when_machine_checks_modify_the_tip_worktree": (15.5, 3, 6),
    "test_train_machine_checks_may_create_runner_artifacts": (5.4, 1, 3),
    "test_train_tree_is_recomputed_from_base_and_heads_at_recovery_and_push": (8.2, 2, 3),
    "test_landed_member_completes_from_immutable_head_despite_mutable_pr_or_issue": (9.2, 2, 5),
    "test_combined_harness_checks_use_all_members_approved_files": (5.6, 1, 3),
    "test_prefix_scope_is_rechecked_at_the_locked_consumption_boundary": (4.1, 1, 2),
    "test_retired_attempt_is_not_rebuilt_after_flow_log_rotation": (5.2, 1, 6),
    "test_prefix_creation_identity_survives_temporary_listing_lag": (4.6, 1, 5),
    "test_batch_github_wait_reaches_owner_pause_without_retirement": (15.5, 3, 3),
    "test_existing_four_member_batch_survives_size_reduction": (7.7, 1, 2),
    "test_last_reconciliation_throttle_stops_batch_preparation": (6.1, 1, 3),
    "test_pending_prefix_reserves_green_members_until_ci_or_deadline": (38.0, 9, 7),
    "test_new_run_after_landed_train_does_not_erase_the_old_generations_merge_fact": (5.0, 1, 11),
    "test_member_final_read_throttle_keeps_train_unspent_and_stops_landing": (7.6, 1, 5),
    "test_worktree_fetch_outage_retires_attempt_without_spending_members": (5.6, 1, 4),
    "test_stale_unlanded_admission_preserves_the_new_running_generation": (1.8, 1, 5),
    "test_union_refresh_checks_and_publishes_its_own_tree_before_ci_reuse": (5.2, 1, 4),
    "test_combined_conflict_markers_are_rejected_by_the_normal_refresh_publication_gate": (4.3, 1, 4),
}


def _tests_in(path):
    """Top-level test functions and Test*::method names with their assert counts."""
    found = {}
    for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            found[node.name] = node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    found[f"{node.name}::{item.name}"] = item
    return {name: sum(isinstance(n, ast.Assert) for n in ast.walk(fn)) for name, fn in found.items()}


def _homes():
    """Where each KNOWN test is defined among the unit test files: name -> [(file, asserts)]."""
    homes = defaultdict(list)
    for rel in unit_files(ROOT):
        for name, asserts in _tests_in(ROOT / rel).items():
            bare = name.split("::")[-1]
            if bare in KNOWN:
                homes[bare].append((rel, asserts))
    return homes


def _shards():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    return int(re.search(r"FA_SHARDS:\s*(\d+)", workflow).group(1))


def test_no_unit_file_or_ci_shard_holds_more_than_two_fifths_of_the_batch_test_time():
    homes = _homes()
    lost = sorted(set(KNOWN) - set(homes))
    doubled = {name: [f for f, _ in where] for name, where in homes.items() if len(where) > 1}
    thinner = {name: (where[0][1], KNOWN[name][2]) for name, where in homes.items()
               if len(where) == 1 and where[0][1] < KNOWN[name][2]}
    assert not lost and not doubled and not thinner, (
        f"batch tests dropped or renamed: {lost}\ndefined in more than one file: {doubled}\n"
        f"fewer assert statements than on main (now, before): {thinner}")

    total = sum(seconds for seconds, _, _ in KNOWN.values())
    per_file = Counter()
    for name, [(rel, _)] in homes.items():
        per_file[rel] += KNOWN[name][0]
    heavy_files = {rel: round(s) for rel, s in per_file.items() if s > SHARE * total}

    shards = _shards()
    files = unit_files(ROOT)
    per_shard = {n: sum(per_file[rel] for rel in select(files, n, shards)) for n in range(shards)}
    heavy_shards = {n: round(s) for n, s in per_shard.items() if s > SHARE * total}
    assert not heavy_files and not heavy_shards, (
        f"recorded batch time {round(total)} s; limit per file or shard {round(SHARE * total)} s\n"
        f"files over it: {heavy_files}\nshards over it (of {shards}): {heavy_shards}\n"
        f"per file: {dict((r, round(s)) for r, s in per_file.items())}")


def test_every_batch_case_is_still_collected_and_no_file_collects_most_of_them():
    homes = _homes()
    files = sorted({rel for where in homes.values() for rel, _ in where})
    assert files, "no unit test file defines the merge-owner batch tests"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=", "-p", "no:cacheprovider",
         "-p", "no:xdist", *files],
        cwd=ROOT, capture_output=True, text=True, timeout=600, check=False)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]

    cases, per_file = Counter(), Counter()
    for line in done.stdout.splitlines():
        if "::" not in line:
            continue
        rel, _, rest = line.partition("::")
        name = rest.split("[")[0].split("::")[-1].strip()
        if name in KNOWN:
            cases[name] += 1
            per_file[rel] += 1
    changed = {name: (cases[name], want) for name, (_, want, _) in KNOWN.items() if cases[name] != want}
    assert not changed, f"collected cases differ from main (now, before): {changed}"

    total = sum(want for _, want, _ in KNOWN.values())
    crowded = {rel: n for rel, n in per_file.items() if n > SHARE * total}
    assert not crowded, (
        f"{total} batch cases; one file may collect at most {int(SHARE * total)}; over it: {crowded}")
