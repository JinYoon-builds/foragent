"""test_launchd.py reaches Foragent code only through public names.

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

UNIT_FILE = Path(__file__).resolve().parents[2] / "tests" / "test_launchd.py"

# Underscore names that are public API of the standard library, not Foragent
# internals (namedtuple methods).
STDLIB_PUBLIC = {"_replace", "_asdict", "_fields", "_make"}

KNOWN_TESTS = (
    "test_label_sanitizing",
    "test_sanitized_label_collisions_have_distinct_hashes",
    "test_plist_round_trip_has_exact_job_and_environment",
    "test_install_creates_directories_and_uses_exact_argv",
    "test_install_preserves_identical_plist_and_replaces_changed_content",
    "test_bootout_not_loaded_is_ignored",
    "test_uninstall_leaves_state_and_github_untouched",
    "test_install_and_uninstall_remove_legacy_job_and_plist",
    "test_legacy_cleanup_error_preserves_legacy_plist",
    "test_colliding_repositories_install_and_uninstall_independently",
    "test_explicit_repo_does_not_require_origin",
    "test_status_reports_presence_loaded_and_last_exit_without_writing",
    "test_real_bootout_error_propagates_and_preserves_plist",
    "test_bootstrap_failure_is_clear",
    "test_missing_launchctl_reports_not_loaded_but_install_fails",
    "test_default_plist_state_paths_and_uid_use_isolated_home",
    "test_running_project_venv_executable_is_preferred",
    "test_fa_executable_prefers_path_shim_over_running_uv_path",
    "test_bare_entrypoint_uses_which_and_python_module_falls_back_to_fa",
    "test_no_fa_executable_has_clear_error",
    "test_default_launchctl_runner_invokes_subprocess_without_shell",
    "test_module_entry_points_forward_dependencies",
    "test_refresh_decision_reads_an_existing_plist",
    "test_restart_adds_gate_environment_while_the_job_is_out",
    "test_install_writes_the_plist_only_after_bootout",
    "test_stale_jobs_finds_every_plist_that_ticks_the_repo",
    "test_refresh_detects_the_executable_even_with_current_environment",
    "test_pinned_uv_path_is_rewritten_and_reloaded_only_between_ticks",
    "test_no_shim_leaves_the_job_unchanged_without_refresh",
    "test_tick_starts_a_detached_refresh_for_any_stale_job_of_the_repo",
    "test_job_state_separates_absent_from_unknown",
    "test_refresh_waits_for_the_tick_then_restarts_every_stale_job_launchd_may_hold",
    "test_refresh_never_rewrites_a_plist_whose_job_state_is_unknown",
    "test_failed_print_during_restart_leaves_the_plist_stale_for_the_next_tick",
    "test_restart_write_failure_after_bootout_still_rolls_back_and_records",
    "test_restart_that_cannot_write_any_definition_is_pending_not_raised_raw",
    "test_bootstrap_success_without_a_loaded_job_records_the_confirmation_error",
    "test_install_failure_after_bootout_records_a_pending_fact",
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


def test_launchd_keeps_its_tests_and_imports_no_private_names():
    _scanner_sees_every_form()
    source = UNIT_FILE.read_text()
    assert _missing_tests(source) == [], "tests renamed or removed"
    found = sorted(private_imports_and_attributes(source), key=_line)
    assert found == [], "tests/test_launchd.py imports or reads private Foragent names:\n" + "\n".join(found)


def test_launchd_keeps_its_tests_and_patches_no_private_names():
    _scanner_sees_every_form()
    source = UNIT_FILE.read_text()
    assert _missing_tests(source) == [], "tests renamed or removed"
    found = sorted(private_patch_targets(source), key=_line)
    assert found == [], "tests/test_launchd.py patches private Foragent names by string:\n" + "\n".join(found)
