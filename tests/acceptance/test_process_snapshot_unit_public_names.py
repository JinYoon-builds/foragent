"""test_process_snapshot.py reaches Foragent code only through public names.

Jin r408 / architect r432 test tidy: a unit test that imports or patches a
private name (``_x``) of a ``foragent`` module pins an internal the owning lane
must stay free to rename. The file must reach production code through public
functions, public module attributes, or I/O boundaries (subprocess, os, the
filesystem, tests/process_fakes.py), and must keep every test it has today.

KNOWN_TESTS was recorded from origin/main on 2026-11-05. If main renames or
adds tests in this file before this lock merges, refresh the list from main.
"""

import ast
import importlib
from pathlib import Path
import re

UNIT_FILE = Path(__file__).resolve().parents[2] / "tests" / "test_process_snapshot.py"

# Underscore names that are public API of the standard library, not Foragent
# internals (namedtuple methods).
STDLIB_PUBLIC = {"_replace", "_asdict", "_fields", "_make"}

KNOWN_TESTS = (
    "test_many_routine_observers_share_one_scan_pair_across_repositories",
    "test_staggered_routine_observers_share_slow_scan_pairs_for_five_seconds",
    "test_child_born_just_after_poll_start_is_witnessed_by_second_following_poll",
    "test_contended_routine_scan_waiter_uses_writer_snapshot_without_scanning",
    "test_contended_routine_scan_lock_has_bounded_direct_fallback",
    "test_stale_or_future_routine_scan_is_replaced_atomically",
    "test_routine_scan_checks_previous_poll_boundary_and_rereads_after_lock",
    "test_backward_wall_clock_step_does_not_extend_routine_scan_reuse",
    "test_invalid_shared_routine_scan_uses_correct_direct_rows",
    "test_shared_routine_cache_io_failure_keeps_direct_behavior",
    "test_shared_routine_unavailable_table_preserves_direct_error",
    "test_shared_routine_cache_unlock_error_does_not_mask_enumeration_error",
    "test_shared_routine_rejects_cached_lifetime_and_adopts_reused_pid_next_scan",
    "test_shared_routine_validates_every_adoption_before_traversing_descendants",
    "test_shared_routine_unexpected_fresh_read_errors_propagate",
    "test_shared_routine_lineage_is_computed_for_each_reader",
    "test_shared_routine_lineage_uses_even_unreconciled_confirmed_ancestors",
    "test_shared_routine_rows_reject_pid_reuse_parent_changes_and_older_children",
    "test_stop_and_launch_observation_never_read_shared_routine_rows",
    "test_pruning_preserves_unreadable_pids_and_unknown_record_fields",
    "test_unavailable_snapshot_is_cached_warns_and_preserves_all_records",
    "test_snapshot_revalidates_a_parent_before_attesting_new_children",
    "test_unreadable_table_still_allows_a_successful_tick",
    "test_pidless_runner_discovery_uses_snapshot_without_ps",
    "test_launch_inside_tick_keeps_its_separate_birth_evidence",
    "test_birth_recorded_by_another_process_after_snapshot_is_not_pruned",
    "test_pruning_rechecks_absent_recorded_identities_before_removal",
    "test_pidless_recovery_scopes_unreadable_processes_to_current_user",
    "test_recovery_confirms_absent_runner_with_fresh_identity",
    "test_shared_stop_checks_recorded_executor_born_after_listing",
    "test_refused_stop_checks_recorded_child_absent_from_routine_snapshot",
    "test_pidless_recovery_uses_only_run_specific_evidence",
    "test_recorded_identity_evidence_distinguishes_cached_absence_from_death",
    "test_recovery_keeps_death_evidence_after_pruning_the_runner_record",
    "test_recovery_preserves_birth_of_runner_launched_and_crashed_inside_tick",
    "test_pidless_recovery_requires_fresh_runner_absence",
    "test_stop_expands_from_a_root_omitted_by_the_tick_snapshot",
    "test_each_stop_discovers_children_forked_after_the_previous_stop",
    "test_legacy_group_discovery_is_fresh_after_its_leader_exits",
    "test_unavailable_legacy_group_discovery_refuses_without_escaping",
    "test_unavailable_tick_snapshot_keeps_explicit_stops_on_the_fresh_path",
    "test_unavailable_table_preserves_orphan_worktree_and_running_label",
)


def _private(name):
    return (name.startswith("_") and not (name.startswith("__") and name.endswith("__"))
            and name not in STDLIB_PUBLIC)


def _bindings(tree):
    """Names bound to foragent modules or objects anywhere in the file."""
    bound, modules = {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "foragent":
                    local = alias.asname or "foragent"
                    bound[local] = alias.name if alias.asname else "foragent"
                    modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "foragent":
            modules.add(node.module)
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
                modules.add(f"{node.module}.{alias.name}")
    return bound, modules


def _foragent_private_names():
    """Private names Foragent itself defines: module and class members and self._x attributes."""
    root = Path(importlib.import_module("foragent").__file__).parent
    names = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        scopes = [tree.body] + [node.body for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        for body in scopes:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(node.name)
                targets = node.targets if isinstance(node, ast.Assign) else (
                    [node.target] if isinstance(node, ast.AnnAssign) else [])
                names |= {t.id for t in targets if isinstance(t, ast.Name)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                    and isinstance(node.value, ast.Name) and node.value.id == "self"):
                names.add(node.attr)
    return {name for name in names if _private(name)}


def _defined_here(tree):
    """Private names the unit file defines itself (its own helpers and fakes)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
              and isinstance(node.value, ast.Name) and node.value.id == "self"):
            names.add(node.attr)
    return names


FORAGENT_PRIVATE = _foragent_private_names()


def private_imports_and_attributes(source):
    tree = ast.parse(source)
    bound, _ = _bindings(tree)
    local = _defined_here(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "foragent":
            found += [f"line {node.lineno}: from {node.module} import {a.name}"
                      for a in node.names if _private(a.name)]
            if any(_private(part) for part in node.module.split(".")):
                found.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            found += [f"line {node.lineno}: import {a.name}" for a in node.names
                      if a.name.split(".")[0] == "foragent" and any(_private(p) for p in a.name.split("."))]
        elif isinstance(node, ast.Attribute) and _private(node.attr):
            root = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if (isinstance(root, ast.Name) and root.id in bound) or (
                    node.attr in FORAGENT_PRIVATE and node.attr not in local):
                found.append(f"line {node.lineno}: {ast.unparse(node)}")
    return found


def private_patch_targets(source):
    """String targets: 'foragent.x._y', or '_y' naming a private attribute of an imported module."""
    tree = ast.parse(source)
    private_names = FORAGENT_PRIVATE - _defined_here(tree)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value
        if re.fullmatch(r"foragent(\.\w+)+", text) and any(_private(p) for p in text.split(".")):
            found.append(f"line {node.lineno}: {text!r}")
        elif text in private_names:
            found.append(f"line {node.lineno}: {text!r}")
    return found


def _line(entry):
    return int(entry.split()[1].rstrip(":"))


def _missing_tests(source):
    present = {node.name for node in ast.walk(ast.parse(source))
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return [name for name in KNOWN_TESTS if name not in present]


def _scanner_sees_every_form():
    sample = (
        "from foragent import processes\n"
        "from foragent.scheduler_stop import _stop_run_processes\n"
        "processes._scan_tree_rows()\n"
        "monkeypatch.setattr(processes, '_routine_scan_lock', None)\n"
        "mock.patch('foragent.processes._read_routine_rows')\n"
        "store._write_run(run)\n"
        "monkeypatch.setattr(store, '_now', clock)\n"
    )
    assert len(private_imports_and_attributes(sample)) == 3
    assert len(private_patch_targets(sample)) == 3
    clean = "from foragent import processes\nprocesses.list_processes()\nmonkeypatch.setattr(processes.os, 'kill', None)\n"
    clean += "class Fake:\n    def __init__(self):\n        self._now = 0\nFake()._now\n"
    assert private_imports_and_attributes(clean) == [] and private_patch_targets(clean) == []


def test_process_snapshot_keeps_its_tests_and_imports_no_private_names():
    _scanner_sees_every_form()
    source = UNIT_FILE.read_text()
    assert _missing_tests(source) == [], "tests renamed or removed"
    found = sorted(private_imports_and_attributes(source), key=_line)
    assert found == [], "tests/test_process_snapshot.py imports or reads private Foragent names:\n" + "\n".join(found)


def test_process_snapshot_keeps_its_tests_and_patches_no_private_names():
    _scanner_sees_every_form()
    source = UNIT_FILE.read_text()
    assert _missing_tests(source) == [], "tests renamed or removed"
    found = sorted(private_patch_targets(source), key=_line)
    assert found == [], "tests/test_process_snapshot.py patches private Foragent names by string:\n" + "\n".join(found)
