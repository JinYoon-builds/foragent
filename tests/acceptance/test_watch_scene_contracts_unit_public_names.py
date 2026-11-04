"""test_watch_scene_contracts.py reaches Foragent code only through public names.

Jin r408 / architect r432 test tidy: a unit test that imports or patches a
private name (``_x``) of a ``foragent`` module pins an internal the owning lane
must stay free to rename. The file must reach production code through public
functions, public module attributes, or I/O boundaries (subprocess, os, the
filesystem, HOME, environment variables, the rendered text), and must keep
every test it has today.

KNOWN_TESTS was recorded from origin/main on 2026-11-05. If main renames or
adds tests in this file before this lock merges, refresh the list from main.
"""

import ast
import importlib
from pathlib import Path
import re

UNIT_FILE = Path(__file__).resolve().parents[2] / "tests" / "test_watch_scene_contracts.py"

# Underscore names that are public API of the standard library, not Foragent
# internals (namedtuple methods).
STDLIB_PUBLIC = {"_replace", "_asdict", "_fields", "_make"}

KNOWN_TESTS = (
    "test_scene_decorations_follow_ascii_mode_without_pixels",
    "test_ascii_templates_preserve_literal_middle_dots_in_names_paths_and_reasons",
    "test_selecting_an_authored_wait_shows_its_canonical_context_not_a_guessed_turn",
    "test_authored_context_keeps_the_recorded_phase_time_and_warning",
    "test_authored_context_keeps_question_and_park_priority_over_an_active_wait",
    "test_selected_authored_wait_uses_the_ascii_mark_when_its_reason_is_clipped",
    "test_shared_elision_bounds_cells_and_marks_forced_omissions",
    "test_fitting_explanation_rows_remove_controls_but_keep_authored_spaces",
    "test_unresolved_authored_lane_keeps_the_recorded_acorn_and_its_selection",
    "test_resolved_authored_lane_still_routes_one_acorn_to_its_recorded_lane",
    "test_the_source_scan_finds_the_phases_of_each_setter",
    "test_every_phase_the_runner_can_set_has_a_word_on_the_acorn",
    "test_a_worker_says_what_it_waits_for_or_that_it_is_preparing",
    "test_an_unknown_phase_or_a_block_shows_the_generic_wait_word_never_blank",
    "test_queued_work_still_says_nothing_beside_its_number",
    "test_a_wait_phase_explains_itself_on_click",
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


def test_watch_scene_contracts_keeps_its_tests_and_imports_no_private_names():
    _scanner_sees_every_form()
    source = UNIT_FILE.read_text()
    assert _missing_tests(source) == [], "tests renamed or removed"
    found = sorted(private_imports_and_attributes(source), key=_line)
    assert found == [], "tests/test_watch_scene_contracts.py imports or reads private Foragent names:\n" + "\n".join(found)


def test_watch_scene_contracts_keeps_its_tests_and_patches_no_private_names():
    _scanner_sees_every_form()
    source = UNIT_FILE.read_text()
    assert _missing_tests(source) == [], "tests renamed or removed"
    found = sorted(private_patch_targets(source), key=_line)
    assert found == [], "tests/test_watch_scene_contracts.py patches private Foragent names by string:\n" + "\n".join(found)
