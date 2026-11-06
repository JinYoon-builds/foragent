"""Acceptance tests for rules-checked: every line in docs/rules.md names the check that keeps it true.

This file is the rules checker itself (PM 11-06 18:20). It reads docs/rules.md, finds the
check each rule line names, and asserts that the named test exists in this repository.
It writes nothing: changed rules text and renamed tests are given to the checker in memory.

Marker form (the lane's choice): a rule line is a `- ` bullet under a `## ` heading. It
ends with `Check:` followed by either `people only` or one or more backticked test
paths, each optionally followed by `::name` (or `::Class::name`), for example
    - Rule text. ([D7](decisions.md#...)) Check: `tests/test_x.py::test_y`.
    - Rule text. ([D110](decisions.md#...)) Check: people only.
A named test is a file `tests/**/test_*.py` that exists; with `::name` the file must define
that test function (or class and method). A parametrize id in `[...]` is ignored.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RULES = "docs/rules.md"
CITE = "([D108](decisions.md#d108-지금-규칙은-rulesmd-지난-기록은-decisionsmd-2026-10-31-jin))"
# A test that exists on main and stays: the control for "names an existing test".
EXISTING = ("tests/acceptance/test_docs_follow_decisions.py::"
            "test_rules_cite_each_new_standing_decision_and_drop_the_retired_skill_rule")
NODE = re.compile(r"`([^`\s:]+\.py)((?:::[^`\s:]+)*)`")
CHECK_FIELD = re.compile(r"\bCheck:\s*(?P<value>.*?)\s*$")
PEOPLE_ONLY = re.compile(r"^people only\.?$")


# ---- the checker ---------------------------------------------------------------------------

def repository_source(path):
    """Text of a repository file, or None when the repository has no such file."""
    if path.startswith("/") or ".." in Path(path).parts:
        return None
    file = ROOT / path
    if not file.is_file():
        return None
    return file.read_text(encoding="utf-8")


def rule_lines(text):
    lines, in_section = [], False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            in_section = True
        elif line.startswith("# "):
            in_section = False
        elif in_section and line.startswith("- "):
            lines.append((number, line))
    return lines


def _defined(source):
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
            names.update(f"{node.name}::{item.name}" for item in node.body
                         if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return names


def _missing(path, name, source):
    """Why the named test is missing, or None when it exists."""
    if not (path.startswith("tests/") and Path(path).name.startswith("test_")):
        return f"{path} is not a test file"
    text = source(path)
    if text is None:
        return f"{path} does not exist"
    if not name:
        return None
    wanted = re.sub(r"\[.*$", "", name)
    if wanted not in _defined(text):
        return f"{path} defines no test {wanted}"
    return None


def problems(text, source=repository_source):
    """Every rule line that names no check or names a test the repository lacks."""
    found = []
    for number, line in rule_lines(text):
        field = CHECK_FIELD.search(line)
        value = field.group("value") if field else ""
        if PEOPLE_ONLY.match(value):
            continue
        nodes = list(NODE.finditer(value))
        if not nodes:
            found.append(f"line {number} names no check: {line}")
            continue
        for match in nodes:
            why = _missing(match.group(1), match.group(2)[2:], source)
            if why:
                found.append(f"line {number} names a missing test `{match.group(0)[1:-1]}` ({why}): {line}")
    return found


# ---- helpers ------------------------------------------------------------------------------

def _rules():
    return (ROOT / RULES).read_text(encoding="utf-8")


def _with_rule(text, line):
    """The rules text with one more rule line before the first rule line."""
    lines = text.splitlines(keepends=True)
    first = rule_lines(text)[0][0] - 1
    lines.insert(first, line + "\n")
    return "".join(lines)


def _report(found):
    return f"{len(found)} problem(s):\n" + "\n".join(found[:25])


@pytest.fixture
def rules():
    """The current docs/rules.md, which must pass the check: the control for every case."""
    text = _rules()
    found = problems(text)
    assert not found, ("docs/rules.md must pass the rules check before a line is added; "
                       + _report(found))
    return text


# ---- statements ---------------------------------------------------------------------------

@pytest.mark.parametrize("ending", ["", " Check:", " Check: .", " Check: to be written.",
                                    " Check: tests/test_order.py."],
                         ids=["no-field", "empty-field", "dot-only", "not-a-test", "unquoted-path"])
def test_rule_line_naming_no_check_fails_the_docs_check(rules, ending):
    sentinel = "ZQX-UNCHECKED-RULE"
    found = problems(_with_rule(rules, f"- {sentinel}: a new rule that names no check. {CITE}{ending}"))
    assert len(found) == 1 and sentinel in found[0], (
        "a rule line that names no check must fail the docs check, naming that line; " + _report(found))


@pytest.mark.parametrize("node", [
    "tests/acceptance/test_docs_follow_decisions.py::test_zqx_missing_rule_check",
    "tests/test_zqx_no_such_file.py::test_zqx_missing_rule_check",
    "tests/test_zqx_no_such_file.py",
    "src/foragent/order.py",
], ids=["missing-test-in-existing-file", "missing-file", "missing-file-path-only", "not-a-test-file"])
def test_rule_line_naming_a_missing_test_fails_the_docs_check(rules, node):
    sentinel = "ZQX-MISSING-RULE"
    found = problems(_with_rule(rules, f"- {sentinel}: a new rule. {CITE} Check: `{node}`."))
    assert len(found) == 1 and sentinel in found[0] and node in found[0], (
        "a rule line that names a test missing from the repository must fail the docs check; "
        + _report(found))


def test_rule_line_naming_an_existing_test_passes_the_docs_check(rules):
    """Control for the missing-test case: the same line naming a real test passes."""
    assert repository_source(EXISTING.split("::")[0]) is not None
    line = f"- ZQX-NAMED-RULE: a new rule. {CITE} Check: `{EXISTING}`."
    assert problems(_with_rule(rules, line)) == []
    gone = problems(_with_rule(rules, line.replace(EXISTING, EXISTING + "_zqx_gone")))
    assert len(gone) == 1 and "ZQX-NAMED-RULE" in gone[0], _report(gone)


def test_renaming_a_test_a_current_rule_line_names_fails_the_docs_check(rules):
    """Removing or renaming any test a current rule line names fails that line.

    A path-only reference is checked by removing or renaming its file; a `::name`
    reference is also checked by renaming the named function in place.
    """
    named = [(number, match) for number, line in rule_lines(rules)
             if CHECK_FIELD.search(line)
             for match in NODE.finditer(CHECK_FIELD.search(line).group("value"))]
    assert named, "no current line of docs/rules.md names a test, so removing a test cannot be caught"
    for number, match in named:
        path = match.group(1)
        moved = str(Path(path).with_name("test_zqx_renamed_" + Path(path).name))

        def without(name, path=path):
            return None if name == path else repository_source(name)

        def renamed_file(name, path=path, moved=moved):
            if name == path:
                return None
            return repository_source(path) if name == moved else repository_source(name)

        for change, source in (("removing", without), ("renaming", renamed_file)):
            found = problems(rules, source)
            assert any(item.startswith(f"line {number} ") for item in found), (
                f"{change} {path} must fail line {number} that names it; " + _report(found))
        if not match.group(2):
            continue
        function = re.sub(r"\[.*$", "", match.group(2).rsplit("::", 1)[-1])
        original = repository_source(path)
        renamed, count = re.subn(rf"\bdef {re.escape(function)}\b", f"def {function}_zqx_renamed",
                                 original)
        assert count, f"{path} does not define {function}"
        found = problems(rules, lambda name, path=path, renamed=renamed:
                         renamed if name == path else repository_source(name))
        assert any(item.startswith(f"line {number} ") for item in found), (
            f"renaming {function} must fail line {number} that names it; " + _report(found))


def test_rule_line_marked_for_people_only_passes_without_a_test(rules):
    sentinel = "ZQX-PEOPLE-RULE"
    line = f"- {sentinel}: a rule for people that no code enforces. {CITE}"
    assert problems(_with_rule(rules, f"{line} Check: people only.")) == [], (
        "a rule line marked for people only must pass without a test")
    unmarked = problems(_with_rule(rules, line))
    assert len(unmarked) == 1 and sentinel in unmarked[0], (
        "the pass must come from the people-only mark, not from the line; " + _report(unmarked))


def test_every_current_rule_line_names_its_check_and_passes():
    text = _rules()
    lines = rule_lines(text)
    assert len(lines) >= 60, "docs/rules.md must keep its rule lines; lines are marked, not deleted"
    found = problems(text)
    assert not found, "every current line of docs/rules.md must name its check; " + _report(found)
