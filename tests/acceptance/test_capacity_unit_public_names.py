"""test_capacity.py reaches Foragent code only through public names.

Jin r408 / architect r432 test tidy: a unit test that imports or patches a
private name (``_x``) of a ``foragent`` module pins an internal the owning lane
must stay free to rename. The file must reach production code through public
functions, public module attributes, or I/O boundaries (subprocess, os, the
filesystem, HOME, environment variables), and must keep every test it has
today.

KNOWN_TESTS was recorded from origin/main on 2026-11-05. If main renames or
adds tests in this file before this lock merges, refresh the list from main.
"""

import ast
import importlib
from pathlib import Path
import re

UNIT_FILE = Path(__file__).resolve().parents[2] / "tests" / "test_capacity.py"

# Underscore names that are public API of the standard library, not Foragent
# internals (namedtuple methods).
STDLIB_PUBLIC = {"_replace", "_asdict", "_fields", "_make"}

KNOWN_TESTS = (
    "test_next_parallel_holds_between_headroom_and_overload",
    "test_next_parallel_never_goes_below_one",
    "test_darwin_pressure_maps_sysctl_levels",
    "test_darwin_pressure_level_reads_kernel_in_process",
    "test_darwin_pressure_reads_sysctl_value_and_handles_errors",
    "test_linux_pressure_reads_proc",
    "test_linux_pressure_without_proc_reads_normal",
    "test_load_per_cpu_divides_by_cpu_count",
    "test_only_the_four_idle_phases_release_machine_slots",
    "test_machine_runs_counts_own_runs_and_live_sibling_runs",
    "test_state_round_trip_and_unreadable_state",
    "test_budget_starts_at_initial_value_and_spaces_increases",
    "test_project_ceiling_limits_the_gate_but_not_the_shared_value",
    "test_budget_demand_excludes_busy_and_paused_lanes",
    "test_machine_runs_counts_a_pidless_sibling_run_only_while_it_may_still_launch",
    "test_machine_runs_counts_fresh_start_reservations_once",
    "test_lanes_cap_limits_the_tick_but_not_the_shared_value",
    "test_memory_pressure_never_moves_the_parallel_budget",
    "test_parallel_floor_in_machine_yml_is_ignored",
    "test_next_test_slots_steps_by_one_within_ceiling",
    "test_next_test_slots_stays_between_one_and_ceiling",
    "test_effective_test_slots_reads_shared_value_capped_by_ceiling",
    "test_update_test_slots_waits_between_increases",
    "test_test_slot_is_exclusive_up_to_the_effective_limit",
    "test_test_slot_held_above_a_lowered_limit_still_counts",
    "test_test_slot_waiter_reports_waiting_then_proceeds",
    "test_test_slot_of_a_killed_holder_is_released",
    "test_test_slot_held_by_the_parent_command_is_not_taken_twice",
    "test_test_slot_stays_unclaimed_while_the_kernel_is_critical",
    "test_test_slot_inherited_by_a_nested_command_ignores_critical",
    "test_linux_swapins_reads_only_swapped_in_pages",
    "test_linux_swapins_unreadable_counter_is_unknown",
    "test_darwin_swapins_uses_mach_64_bit_counter_and_releases_host_port",
    "test_machine_load_rebases_after_unreadable_sample_and_corrupt_state",
    "test_cpu_busy_accounts_for_cpu_time_categories",
    "test_cpu_busy_does_not_count_guest_time_twice",
    "test_cpu_busy_rebases_when_window_has_no_progress_or_counters_reset",
    "test_cpu_busy_identical_counters_reuse_persisted_fraction",
    "test_cpu_busy_short_windows_accumulate_without_extreme_fractions",
    "test_cpu_busy_is_disabled_in_fresh_test_processes",
    "test_cpu_busy_unavailable_counter_is_unknown",
    "test_machine_load_preserves_cpu_and_swapin_baselines_together",
    "test_machine_load_keeps_raw_load_as_the_upper_bound",
    "test_budget_decreases_are_rate_limited_across_repositories",
    "test_test_slots_decrease_immediately_after_growth_and_retain_decrease_clock",
    "test_memory_pressure_never_moves_test_slots",
    "test_test_slots_floor_in_machine_yml_is_ignored",
    "test_machine_max_parallel_caps_the_shared_value_during_decrease_cooldown",
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


def test_capacity_keeps_its_tests_and_imports_no_private_names():
    _scanner_sees_every_form()
    source = UNIT_FILE.read_text()
    assert _missing_tests(source) == [], "tests renamed or removed"
    found = sorted(private_imports_and_attributes(source), key=_line)
    assert found == [], "tests/test_capacity.py imports or reads private Foragent names:\n" + "\n".join(found)


def test_capacity_keeps_its_tests_and_patches_no_private_names():
    _scanner_sees_every_form()
    source = UNIT_FILE.read_text()
    assert _missing_tests(source) == [], "tests renamed or removed"
    found = sorted(private_patch_targets(source), key=_line)
    assert found == [], "tests/test_capacity.py patches private Foragent names by string:\n" + "\n".join(found)
