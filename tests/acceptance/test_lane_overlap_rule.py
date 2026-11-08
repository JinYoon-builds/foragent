"""Acceptance tests for n12-lane-overlap-rule: the current lane rule matches per-file overlap limits.

The public surface is the repository's current-rule document (docs/rules.md), its link into the
append-only decision history (docs/decisions.md) and the repository's current-rule validation
(the pytest checks that read docs/rules.md). Nothing here writes to the repository.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

from test_rules_checked import CHECK_FIELD, NODE, problems, rule_lines

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "docs" / "rules.md"
DECISIONS = ROOT / "docs" / "decisions.md"

# The decision already recorded on main (docs/decisions.md, 2026-11-05 answer to r266).
DECISION_MARK = "answers r266"
DECISION_TEXT = "while each file stays within its overlap limit"

# Existing tests on main that show two or more issues sharing a file running together while
# the file stays within its overlap limit, and a further one waiting at that limit.
BOUNDED_SHARED = {
    "tests/acceptance/test_optimistic_overlap.py": {
        "test_optimistic_overlap_starts_two_and_waits_the_third_without_wasting_a_slot",
        "test_optimistic_overlap_configured_limit_is_used_by_the_scheduler",
        "test_optimistic_overlap_counts_each_file_independently_and_honors_machine_capacity",
        "test_optimistic_overlap_per_file_limit_applies_across_lanes",
    },
    "tests/acceptance/test_measured_overlap_plan.py": {
        "test_recorded_file_plan_allows_fifth_run_without_releasing_exclusive_claims",
    },
    "tests/test_overlap_plan_unit.py": {
        "test_file_limit_replaces_the_fallback_only_for_its_file",
        "test_file_limit_counts_the_candidate_and_can_lower_the_fallback",
    },
}
# Existing tests on main that show an issue without Files waiting for and then occupying its lane alone.
NO_FILES = {
    "tests/acceptance/test_lane_width_by_overlap.py": {
        "test_lane_width_empty_files_wait_for_and_exclusively_occupy_the_lane",
    },
    "tests/test_overlap_files.py": {
        "test_no_files_keeps_lane_serialization_and_an_unheld_issue_records_nothing",
        "test_unknown_and_empty_files_hold_only_their_own_lane",
    },
}

STALE = re.compile(r"(don['’]t|do not|doesn['’]t|never) overlap|disjoint|must not share", re.I)
LANE_RULE = re.compile(r"\blanes?\b", re.I)
LIMIT = re.compile(r"overlap limit", re.I)
NO_FILES_TEXT = re.compile(r"(no|without) `?files`?", re.I)
ALONE = re.compile(r"one at a time|alone|by itself|exclusive", re.I)
LINK = re.compile(r"\]\(decisions\.md#([^)\s]+)\)")


# ---- helpers ------------------------------------------------------------------------------

def _slug(heading):
    """GitHub's heading anchor: lower case, punctuation dropped, spaces become hyphens."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _sections(decisions):
    """anchor -> text of that heading's section (until the next heading)."""
    sections, anchor, body = {}, None, []
    for line in decisions.splitlines():
        if line.startswith("#"):
            if anchor is not None:
                sections.setdefault(anchor, "\n".join(body))
            anchor, body = _slug(line.lstrip("#")), []
        elif anchor is not None:
            body.append(line)
    if anchor is not None:
        sections.setdefault(anchor, "\n".join(body))
    return sections


def _resolves_to_decision(anchor, decisions):
    lines = decisions.splitlines()
    line_anchor = re.fullmatch(r"L(\d+)", anchor)
    if line_anchor:
        number = int(line_anchor.group(1))
        return 0 < number <= len(lines) and DECISION_MARK in lines[number - 1]
    return DECISION_MARK in _sections(decisions).get(anchor, "")


def _lane_rule_lines(rules):
    """Rule bullets about running several issues of a lane at once."""
    found = []
    for number, line in rule_lines(rules):
        text = CHECK_FIELD.split(line)[0] if CHECK_FIELD.search(line) else line
        if LANE_RULE.search(text) and re.search(r"files?", text, re.I) and (
                re.search(r"at once|concurren|simultaneous|in parallel", text, re.I)
                or NO_FILES_TEXT.search(text)):
            found.append((number, line, text))
    return found


def _current_rule(rules):
    candidates = [item for item in _lane_rule_lines(rules) if LIMIT.search(item[2])]
    assert len(candidates) == 1, (
        "docs/rules.md must hold exactly one current lane-concurrency rule that names the per-file "
        f"overlap limit; found {len(candidates)}: {[line for _, line, _ in _lane_rule_lines(rules)]}")
    return candidates[0]


def _named(line):
    field = CHECK_FIELD.search(line)
    assert field, f"the lane rule names no Check: {line}"
    nodes = []
    for match in NODE.finditer(field.group("value")):
        name = re.sub(r"\[.*$", "", match.group(2)[2:]) if match.group(2) else ""
        nodes.append((match.group(1), name.rsplit("::", 1)[-1] if name else ""))
    assert nodes, f"the lane rule's Check names no test: {line}"
    return nodes


def _covers(nodes, table):
    hits = []
    for path, name in nodes:
        wanted = table.get(path, set())
        if (name and name in wanted) or (not name and wanted):
            hits.append((path, name))
    return hits


def _defines(path, name):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return any(isinstance(node, ast.FunctionDef) and node.name == name for node in tree.body)


# ---- statement 1: rule text ---------------------------------------------------------------

def test_current_lane_rule_permits_shared_files_within_the_overlap_limit():
    rules = RULES.read_text(encoding="utf-8")
    stale = [line for _, line, text in _lane_rule_lines(rules) if STALE.search(text)]
    assert not stale, f"a current lane rule still requires disjoint files: {stale}"
    _, line, text = _current_rule(rules)
    assert re.search(r"each file|per[- ]file", text, re.I), (
        f"the lane rule must bound concurrency per file, not per lane: {line}")
    assert re.search(r"any number|several|multiple|more than one", text, re.I), (
        f"the lane rule must allow more than one issue of a lane at once: {line}")


def test_current_lane_rule_keeps_issues_without_files_alone_in_the_lane():
    rules = RULES.read_text(encoding="utf-8")
    _, line, text = _current_rule(rules)
    assert NO_FILES_TEXT.search(text) and ALONE.search(text), (
        f"the lane rule must keep exclusive lane execution for issues without Files: {line}")


def test_current_lane_rule_links_to_the_recorded_decision_without_recording_it_again():
    rules = RULES.read_text(encoding="utf-8")
    decisions = DECISIONS.read_text(encoding="utf-8")
    _, line, text = _current_rule(rules)
    anchors = LINK.findall(text)
    assert any(_resolves_to_decision(anchor, decisions) for anchor in anchors), (
        "the lane rule must link to the decision already recorded in docs/decisions.md "
        f"(the 2026-11-05 answer to r266); its links resolve to {anchors}: {line}")
    assert decisions.count(DECISION_MARK) == 1, "the r266 decision must be recorded exactly once"
    assert decisions.count(DECISION_TEXT) == 1, (
        "the overlap-limit decision text must not be appended to the decision history again")


# ---- statement 2: checks and validation ---------------------------------------------------

def test_lane_rule_checks_name_existing_bounded_overlap_and_no_files_tests():
    rules = RULES.read_text(encoding="utf-8")
    number, line, _ = _current_rule(rules)
    nodes = _named(line)
    missing = [item for item in problems(rules) if item.startswith(f"line {number} ")]
    assert not missing, f"the lane rule names tests the repository lacks: {missing}"
    bounded = _covers(nodes, BOUNDED_SHARED)
    alone = _covers(nodes, NO_FILES)
    assert bounded, (
        "the lane rule's Check must name an existing test of shared files running together within "
        f"their overlap limit (one of {sorted(BOUNDED_SHARED)}); it names {nodes}")
    assert alone, (
        "the lane rule's Check must name an existing test of an issue without Files running alone "
        f"(one of {sorted(NO_FILES)}); it names {nodes}")
    for path, name in bounded + alone:
        if name:
            assert _defines(path, name), f"{path} no longer defines {name}"
    selected = [f"{path}::{name}" if name else path for path, name in bounded + alone]
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *selected],
                         cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert run.returncode == 0, (
        f"the tests the lane rule names must pass ({selected}):\n{run.stdout[-3000:]}{run.stderr[-2000:]}")


def test_repository_current_rule_validation_exits_zero_with_the_updated_rule():
    rules = RULES.read_text(encoding="utf-8")
    _current_rule(rules)  # the validation must run on the updated rule, not the stale one
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/acceptance/test_rules_checked.py", "tests/acceptance/test_docs_follow_decisions.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert run.returncode == 0, f"current-rule validation failed:\n{run.stdout[-3000:]}{run.stderr[-2000:]}"


# ---- break-it controls on the updated rule ------------------------------------------------

def _replace_rule(rules, number, new_line):
    lines = rules.splitlines()
    lines[number - 1] = new_line
    return "\n".join(lines) + "\n"


def test_stale_disjoint_wording_or_dropped_link_breaks_the_rule_checks():
    rules = RULES.read_text(encoding="utf-8")
    decisions = DECISIONS.read_text(encoding="utf-8")
    number, line, text = _current_rule(rules)
    # Wrong value: the old disjoint-files wording.
    wrong = _replace_rule(rules, number, line.replace(
        text, text.rstrip() + " Issues run together only if their `files` don't overlap. ", 1))
    assert any(STALE.search(t) for _, _, t in _lane_rule_lines(wrong))
    reverted = _replace_rule(rules, number, LIMIT.sub("disjoint files rule", line))
    assert not [item for item in _lane_rule_lines(reverted) if LIMIT.search(item[2])]
    # Skipped effect: the link to the recorded decision removed.
    unlinked = LINK.sub("]()", text)
    assert not any(_resolves_to_decision(a, decisions) for a in LINK.findall(unlinked))
    # Doubled effect: the decision appended to the history a second time.
    doubled = decisions + "\n" + next(l for l in decisions.splitlines() if DECISION_MARK in l) + "\n"
    assert doubled.count(DECISION_MARK) == 2
    # Missing check: the no-Files test dropped from the Check entries.
    nodes = _named(line)
    assert _covers([n for n in nodes if not _covers([n], NO_FILES)], NO_FILES) == []
