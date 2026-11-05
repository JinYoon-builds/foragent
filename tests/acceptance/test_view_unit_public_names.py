"""tests-public-names-view: the view lane's unit tests import only public Foragent names.

Structural cleanup order A part 5 (2026-11-05). A unit test that imports a private
(leading underscore) name of a ``foragent`` module pins an internal, so removing or
renaming code breaks tests whose behaviour did not change. The view lane's unit test
files (tests/test_watch*.py, tests/test_status*.py, tests/test_notify*.py,
tests/test_i18n*.py) and the tests/helpers/** modules they import must reach Foragent
through public names or the CLI, keep every test name, keep passing, and still fail
when the behaviour they check is broken.

The break-it cases copy src/ to a temporary folder, change one line of product code
and run the named unit test against that copy: the test must fail.
"""

import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ElementTree

import pytest

from view_unit_known_tests import KNOWN_TESTS

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
PATTERNS = ("test_watch*.py", "test_status*.py", "test_notify*.py", "test_i18n*.py")

IMPORT_CALLS = {"import_module", "__import__", "importorskip"}


def view_unit_files():
    return sorted({path for pattern in PATTERNS for path in TESTS.glob(pattern)})


def _private(part):
    return part.startswith("_") and not (part.startswith("__") and part.endswith("__"))


def _foragent_private(dotted):
    parts = dotted.split(".")
    return parts[0] == "foragent" and any(_private(part) for part in parts[1:])


def private_imports(source):
    """Every way the source imports a leading-underscore name or module from foragent."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "foragent":
            if _foragent_private(node.module):
                found.append(f"line {node.lineno}: from {node.module} import ...")
            found += [f"line {node.lineno}: from {node.module} import {alias.name}"
                      for alias in node.names if _private(alias.name)]
        elif isinstance(node, ast.Import):
            found += [f"line {node.lineno}: import {alias.name}"
                      for alias in node.names if _foragent_private(alias.name)]
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name in IMPORT_CALLS and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) and _foragent_private(node.args[0].value):
                found.append(f"line {node.lineno}: {name}({node.args[0].value!r})")
    return found


def helper_modules(source):
    """tests/helpers/** modules the source imports (helpers.x, from helpers import x, ...)."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        for name in names:
            if name.split(".")[0] != "helpers":
                continue
            base = TESTS.joinpath(*name.split("."))
            for path in (base.with_suffix(".py"), base / "__init__.py"):
                if path.is_file():
                    found.add(path)
    return found


def file_findings(path):
    """Private imports of a unit file and, transitively, of the helper modules it imports."""
    findings, seen, todo = [], set(), [path]
    while todo:
        current = todo.pop()
        if current in seen:
            continue
        seen.add(current)
        source = current.read_text(encoding="utf-8")
        findings += [f"{current.relative_to(REPO)} {entry}" for entry in private_imports(source)]
        todo += sorted(helper_modules(source) - seen)
    return findings


def collected_test_names(source):
    tree = ast.parse(source)
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names |= {f"{node.name}::{item.name}" for item in node.body
                      if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return names


def _scanner_sees_every_form():
    sample = (
        "from foragent.watch_layout import _activity\n"
        "from foragent.commands import _pm_table\n"
        "from foragent.watch import _Refresh as Refresh\n"
        "from foragent._build import GIT_SHA\n"
        "import foragent.watch_sections._private as hidden\n"
        "def test_x():\n"
        "    from foragent.watch_cells import _cells\n"
        "    importlib.import_module('foragent.commands._pm_table')\n"
    )
    assert len(private_imports(sample)) == 7, private_imports(sample)
    clean = (
        "from foragent import watch, statusline\n"
        "from foragent.watch import render_frame, __version__\n"
        "from foragent.watch_sections import lane_blocks\n"
        "import foragent.watch_layout\n"
        "from test_watch import _snapshot\n"
        "def _helper():\n"
        "    importlib.import_module('foragent.watch')\n"
    )
    assert private_imports(clean) == []


def test_no_view_lane_unit_test_imports_a_private_foragent_name():
    _scanner_sees_every_form()
    files = view_unit_files()
    assert set(KNOWN_TESTS) == {path.name for path in files}, "refresh the view lane's known test inventory"
    found = [entry for path in files for entry in file_findings(path)]
    assert found == [], "view lane unit tests import private Foragent names:\n" + "\n".join(found)


@pytest.mark.parametrize("name", sorted(KNOWN_TESTS))
def test_view_unit_file_keeps_its_tests_and_imports_only_public_names(name):
    path = TESTS / name
    assert path.is_file(), f"tests/{name} was removed"
    source = path.read_text(encoding="utf-8")
    missing = [test for test in KNOWN_TESTS[name] if test not in collected_test_names(source)]
    assert missing == [], f"tests/{name} lost or renamed tests: {missing}"
    found = file_findings(path)
    assert found == [], f"tests/{name} imports private Foragent names:\n" + "\n".join(found)


def _environment(pythonpath=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    if pythonpath is not None:
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(pythonpath)] + ([environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else []))
    return environment


def run_unit(tmp_path, name, select=None, pythonpath=None):
    """Run one unit file (optionally one test) in a child pytest; return {test name: [outcomes]}."""
    report = tmp_path / f"{Path(name).stem}-{len(list(tmp_path.glob('*.xml')))}.xml"
    target = f"tests/{name}" + (f"::{select}" if select else "")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "-p", "no:xdist",
         "-p", "no:cacheprovider", "-p", "no:randomly",
         f"--junitxml={report}", target],
        cwd=REPO, env=_environment(pythonpath), capture_output=True, text=True, timeout=900,
    )
    outcomes = {}
    if report.is_file():
        for case in ElementTree.parse(report).getroot().iter("testcase"):
            base = case.get("name", "").split("[")[0]
            klass = case.get("classname", "").rsplit(".", 1)[-1]
            key = f"{klass}::{base}" if klass.startswith("Test") else base
            kinds = [child.tag for child in case if child.tag in ("failure", "error", "skipped")]
            outcomes.setdefault(key, []).append(kinds[0] if kinds else "passed")
    return outcomes, result


@pytest.mark.parametrize("name", sorted(KNOWN_TESTS))
def test_cleaned_view_unit_file_keeps_every_test_passing(tmp_path, name):
    path = TESTS / name
    missing = [test for test in KNOWN_TESTS[name] if test not in collected_test_names(path.read_text(encoding="utf-8"))]
    assert missing == [], f"tests/{name} lost or renamed tests: {missing}"
    outcomes, result = run_unit(tmp_path, name)
    assert outcomes, result.stdout[-4000:] + result.stderr[-2000:]
    not_run = [test for test in KNOWN_TESTS[name] if test not in outcomes]
    failing = {test: kinds for test, kinds in outcomes.items()
               if test in KNOWN_TESTS[name] and any(kind != "passed" for kind in kinds)}
    assert result.returncode == 0 and not_run == [] and failing == {}, (not_run, failing, result.stdout[-4000:])


# (case id, unit file, test, product file under src/foragent, [(old text, new text)])
BREAKS = [
    ("pm-table-row-doubled", "test_statusline_pm_table.py", "test_summary_off_still_gets_one_table",
     "commands/_pm_table.py", [('"\\n".join([*rows, "Use these', '"\\n".join([*rows, *rows, "Use these')]),
    ("pm-table-rows-skipped", "test_statusline_pm_table.py",
     "test_option_rename_and_removal_follow_registered_actions",
     "commands/_pm_table.py", [('"\\n".join([*rows, "Use these', '"\\n".join([*rows[:0], "Use these')]),
    ("wait-phase-minutes-wrong", "test_watch.py", "test_overlap_wait_activity_uses_translated_waiting_phase",
     "watch_layout.py", [("minutes=int((phase_seconds + age) // 60))", "minutes=int((phase_seconds + age) // 30))")]),
    ("box-wrap-loses-spaces", "test_watch_acorns.py", "test_a_wrapped_box_line_joins_back_to_the_exact_text",
     "watch_sections/acorns.py", [("pieces.append(text[:cut])", "pieces.append(text[:cut].rstrip(' ') or text[:cut])")]),
    ("omitted-count-wrong", "test_watch_scene_review.py",
     "test_oversized_first_acorns_still_leave_room_for_the_exact_omitted_count",
     "watch_sections/lane_blocks.py", [('f"{mark}{total - index}", "dim"', 'f"{mark}{total - index + 1}", "dim"')]),
    ("omitted-count-beside-id-wrong", "test_watch_scene_review.py",
     "test_an_id_that_fits_beside_the_count_is_drawn_not_counted",
     "watch_sections/lane_blocks.py", [('f"{mark}{total - index}", "dim"', 'f"{mark}{total - index + 1}", "dim"')]),
    ("refresh-skips-saved-config", "test_watch_settings.py",
     "test_quota_save_shows_on_the_last_line_before_and_after_the_next_snapshot",
     "watch.py", [("self.read, self.config = read, config", "self.read, self.config, self.first = read, config, config"),
                  ("return self.read(self.config)", "return self.read(self.first)")]),
    ("footer-overflows-width", "test_watch_usage.py",
     "test_a_narrow_frame_wraps_the_footer_between_items_instead_of_losing_its_end",
     "watch_sections/usage.py", [("watch._cells(joined) <= width:", "watch._cells(joined) <= width + 12:"),
                                 ("watch._cells(row) <= width else", "watch._cells(row) <= width + 12 else")]),
]


def mutated_source(tmp_path, product, edits):
    copy = tmp_path / "src"
    shutil.copytree(REPO / "src", copy, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    target = copy / "foragent" / product
    text = target.read_text(encoding="utf-8")
    for old, new in edits:
        assert text.count(old) == 1, f"break-it anchor {old!r} not found once in src/foragent/{product}"
        text = text.replace(old, new)
    target.write_text(text, encoding="utf-8")
    probe = subprocess.run([sys.executable, "-c", "import foragent; print(foragent.__file__)"],
                           cwd=REPO, env=_environment(copy), capture_output=True, text=True, timeout=120)
    assert Path(probe.stdout.strip()).resolve().is_relative_to(copy.resolve()), probe.stdout + probe.stderr
    return copy


@pytest.mark.parametrize("case,name,test,product,edits", BREAKS, ids=[case[0] for case in BREAKS])
def test_breaking_the_checked_behaviour_still_fails_the_cleaned_test(tmp_path, case, name, test, product, edits):
    assert test in collected_test_names((TESTS / name).read_text(encoding="utf-8")), f"tests/{name}::{test} was renamed"
    intact, result = run_unit(tmp_path, name, test)
    assert result.returncode == 0 and intact.get(test) and set(intact[test]) == {"passed"}, (
        intact, result.stdout[-3000:])
    copy = mutated_source(tmp_path, product, edits)
    broken, result = run_unit(tmp_path, name, test, pythonpath=copy)
    assert broken.get(test), (broken, result.stdout[-3000:] + result.stderr[-2000:])
    assert result.returncode == 1 and len(broken[test]) == len(intact[test]) and set(broken[test]) == {"failure"}, (
        f"breaking src/foragent/{product} ({case}) no longer fails tests/{name}::{test}: {broken[test]}")
