"""Acceptance for n23-early-loop-facts: running review loops beside size and PR line counts.

`fa orch facts` (human and --json) must show a running issue's declared size, its
recorded usable reviewer rounds and the observed PR additions/deletions together,
before the run finishes or reaches ten rounds. The issue runner is the only writer
of the PR measurement, so the main scenario drives the public runner (`run_issue`)
on a real offline Git history whose branch adds 900 lines and deletes 100, and reads
the facts from inside the seventh reviewer call, while the run is still in its loop.
A run with no PR measurement must show its line counts as unknown, never zero.
Reading facts must leave labels, priority, run state and architect requests unchanged.

The JSON schema of the new fields is not prescribed: a record about the issue must
hold the size, the rounds and keys naming additions and deletions.
"""

from datetime import UTC, datetime, timedelta
import json
import os
import re

import pytest

from foragent.executors import ExecResult
from foragent.runner import run_issue
from test_orch_review_handoff import bind_cli, make_case
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


ADDED, DELETED, ROUNDS = 900, 100, 6
LEGACY = "legacy.txt"
ADDITION_KEY = re.compile(r"(?i)addition|added|insertion|inserted")
DELETION_KEY = re.compile(r"(?i)deletion|deleted|removed|removal")
ROUND_KEY = re.compile(r"(?i)round")
# A whole line-count field (e.g. pr_lines: null); "baseline" and similar words do not count.
LINES_KEY = re.compile(r"(?i)(?<![a-z])(pr_?)?lines?(?![a-z])|line_?counts?|diff_?stat|pr_?diff|pr_?changes?")


# ---------------------------------------------------------------- JSON helpers

def _issue_of(item):
    for key in ("issue", "number"):
        if type(item.get(key)) is int:
            return item[key]
    return None


def issue_records(value, number):
    """Every dict in the facts that names `number` as its issue (incl. string keys like {"1": {...}})."""
    found = []

    def walk(item, keyed=None):
        if isinstance(item, dict):
            if _issue_of(item) == number or keyed == str(number):
                found.append(item)
            for key, child in item.items():
                walk(child, key if isinstance(key, str) else None)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return found


def leaves(item, path=()):
    if isinstance(item, dict):
        for key, child in item.items():
            yield from leaves(child, (*path, str(key)))
    elif isinstance(item, list):
        for index, child in enumerate(item):
            yield from leaves(child, path)
    else:
        yield path, item


def _values(record, pattern):
    """Leaf values whose own key (last path part) matches pattern; also a parent key for scalar-only maps."""
    return [value for path, value in leaves(record) if path and pattern.search(path[-1])]


def _has_size_s(record):
    return any(path and "size" in path[-1].lower() and value == "s" for path, value in leaves(record))


def _has_rounds(record, rounds):
    return any(type(value) is int and value == rounds for value in _values(record, ROUND_KEY))


def joined_record(facts, number):
    """The one record about `number` that holds size s, the rounds and both line counts."""
    records = issue_records(facts, number)
    assert records, f"fa orch facts --json names no record for running issue #{number}"
    return records


def _line_counts(record):
    added = [value for value in _values(record, ADDITION_KEY) if not isinstance(value, bool)]
    deleted = [value for value in _values(record, DELETION_KEY) if not isinstance(value, bool)]
    return added, deleted


# ---------------------------------------------------------------- shared fixture

def loop_case(git_history, tmp_path, monkeypatch):
    from conftest import git_command
    from foragent import resource_sample

    # Base has a 100-line file the branch deletes, so the PR shows real deletions.
    git_history.update({LEGACY: "".join(f"legacy {index}\n" for index in range(DELETED))})
    case = make_case(git_history, tmp_path, monkeypatch, reviewer="codex", executor="claude",
                     skill_libraries=[], test_command="true", language="en",
                     review_max=12, max_retries=12)
    git_command(case.root, "fetch", "origin")
    labels = ["approved", "running", "size:s", "priority:high"]
    case.tracker.add_labels(1, ["size:s", "priority:high"])
    case.store.update_local_issue(1, title="Implement feature", body=case.tracker.get_issue(1).body,
                                  labels=labels, state="open")
    resource_sample.record(case.store, ("normal", 0.1), case.clock[0])
    case.main = bind_cli(case, monkeypatch)
    return case


def implement_rounds(case):
    def first(kwargs):
        worktree = kwargs["worktree"]
        (worktree / "feature.txt").write_text("".join(f"feature {index}\n" for index in range(ADDED)))
        (worktree / LEGACY).unlink()
        return "Implemented"

    def rework(kwargs):
        # Rewrite an already added line: the PR still adds 900 and deletes 100 lines.
        path = kwargs["worktree"] / "feature.txt"
        lines = path.read_text().splitlines(keepends=True)
        lines[0] = f"feature reworked {len(case.implementer.calls)}\n"
        path.write_text("".join(lines))
        return "Reworked"

    case.implementer.steps = [first] + [rework] * 20


def snapshot(case):
    from foragent import orch_inbox

    issue = case.tracker.get_issue(1)
    run = case.store.get_run(1)
    local = next(row for row in case.store.local_issues() if row["number"] == 1)
    try:
        requests = orch_inbox.requests(case.store)
    except Exception as error:  # noqa: BLE001 - a missing inbox is an empty one
        requests = repr(error)
    return {
        "tracker_labels": sorted(issue.labels),
        "local_labels": sorted(local.get("labels") or ()),
        "run": (run.status, run.phase, run.attempts, run.started_at),
        "requests": json.dumps(requests, sort_keys=True, default=str),
        "events": [line for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()
                   if "architect" in line or "role" in line],
    }


def read_facts(case, capsys, *, human):
    capsys.readouterr()
    argv = ["orch", "facts", "--repo-root", str(case.root)] + ([] if human else ["--json"])
    code = cli_exit(case.main, argv)
    output = capsys.readouterr()
    assert code == 0, f"fa orch facts failed during the review loop: {output.err.strip()}"
    return output.out if human else json.loads(output.out)


def install_reviewer(case, review):
    """Scripted reviewer; each scoring session confirms its finding under a distinct class,
    so six different defects never look like one repeated design problem."""
    from types import SimpleNamespace

    from test_orch_review_handoff import CONFIRMED_SCORE, scoring_request

    scorings = []

    def run(**kwargs):
        if scoring_request(kwargs["prompt"]) is not None:
            scorings.append(kwargs)
            answer = dict(json.loads(CONFIRMED_SCORE), finding_class=f"scripted defect {len(scorings)}")
            return ExecResult(0, False, json.dumps(answer), kwargs["log_path"])
        return review(**kwargs)

    reviewer = SimpleNamespace(run=run)
    implementer, executor = case.implementer, case.config.executor
    case.ctx.executor_factory = lambda name: implementer if name == executor else reviewer


def run_loop_and_observe(case, capsys):
    """Six FAIL rounds; at the seventh reviewer call read both facts forms, then stop the run."""
    implement_rounds(case)
    observed = {}
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        if len(calls) <= ROUNDS:
            text = (f"VERDICT: FAIL\nfeature.txt:{len(calls)} — defect number {len(calls)} "
                    f"loses data in path {len(calls)}\n  class: defect-{len(calls)}\n")
            return ExecResult(0, False, text, kwargs["log_path"])
        if not observed:
            assert case.store.get_run(1).status == "running", "The issue must still be running"
            before = snapshot(case)
            observed["json"] = read_facts(case, capsys, human=False)
            observed["human"] = read_facts(case, capsys, human=True)
            observed["unchanged"] = (before, snapshot(case))
        return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

    install_reviewer(case, review)
    run_issue(1, case.ctx)
    assert observed, (f"The fixture must reach a seventh reviewer call after {ROUNDS} FAIL rounds; "
                      f"reviewer calls: {len(calls)}")
    return observed


# ---------------------------------------------------------------- statement 1

def test_running_loop_json_joins_size_rounds_and_pr_lines(git_history, tmp_path, monkeypatch, capsys):
    case = loop_case(git_history, tmp_path, monkeypatch)
    observed = run_loop_and_observe(case, capsys)
    records = joined_record(observed["json"], 1)
    joined = [record for record in records
              if _has_size_s(record) and _has_rounds(record, ROUNDS) and ADDED in _line_counts(record)[0]
              and DELETED in _line_counts(record)[1]]
    assert joined, (
        "No single facts record for running issue #1 joins size s, 6 usable reviewer rounds and the PR's "
        f"900 additions / 100 deletions: {json.dumps(records, default=str)[:3000]}")
    added, deleted = _line_counts(joined[0])
    assert 0 not in added and 0 not in deleted and None not in added and None not in deleted


def test_running_loop_human_line_joins_size_rounds_and_pr_lines(git_history, tmp_path, monkeypatch, capsys):
    case = loop_case(git_history, tmp_path, monkeypatch)
    observed = run_loop_and_observe(case, capsys)
    human = observed["human"]
    lines = [line for line in human.splitlines()
             if re.search(r"#1\b", line) and re.search(r"(?i)\bsize\W{0,3}s\b", line)
             and re.search(rf"(?<![\d.]){ADDED}(?![\d.])", line)
             and re.search(rf"(?<![\d.]){DELETED}(?![\d.])", line)
             and re.search(rf"(?i)(?<![\d.]){ROUNDS}(?![\d.])\s*(review\s*)?rounds?|rounds?\W{{0,3}}{ROUNDS}\b", line)]
    assert lines, ("The human facts need one line for running #1 with size s, 6 rounds, 900 additions "
                   "and 100 deletions:\n" + human)


# ---------------------------------------------------------------- statement 3

def test_reading_loop_facts_changes_no_labels_priority_run_state_or_requests(
        git_history, tmp_path, monkeypatch, capsys):
    case = loop_case(git_history, tmp_path, monkeypatch)
    observed = run_loop_and_observe(case, capsys)
    before, after = observed["unchanged"]
    assert after == before, "Reading loop facts must not change labels, priority, run state or requests"
    assert "priority:high" in before["tracker_labels"] and "size:s" in before["local_labels"]
    # Positive control: the facts read happened and showed the loop (statement 1 evidence present).
    records = issue_records(observed["json"], 1)
    assert any(ADDED in _line_counts(record)[0] for record in records), \
        "The unchanged check is only meaningful when the facts showed the PR line counts"


# ---------------------------------------------------------------- statement 2

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def recorded_loop(tmp_path, monkeypatch, number=7, rounds=ROUNDS):
    """A running size s issue with recorded usable rounds and no PR measurement at all."""
    from foragent import orch_events
    from test_orch_facts import facts_case

    case = facts_case(tmp_path, monkeypatch)
    store, clock = case.store, case.clock
    clock[0] = NOW - timedelta(hours=1)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number), lane="roles")
    store.update_local_issue(number, title=f"Task {number}", body="",
                             labels=["approved", "running", "size:s"], state="open")
    for round_number in range(1, rounds + 1):
        at = clock[0] + timedelta(minutes=round_number)
        path = store.root / "runs" / f"issue-{number}" / f"review-{round_number}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"VERDICT: FAIL\nfeature.txt:{round_number} — defect {round_number}\n"
                        f"  class: defect-{round_number}\n")
        os.utime(path, (at.timestamp(), at.timestamp()))
        orch_events.append_event(store, at, number, "review", "FAIL 1 finding", verdict="FAIL",
                                 findings=1, round=round_number, source="reviewer")
    clock[0] = NOW
    return case


def test_missing_pr_measurement_is_unknown_not_zero_in_json(tmp_path, monkeypatch, capsys):
    from test_orch_facts import facts

    case = recorded_loop(tmp_path, monkeypatch)
    result = facts(case, capsys)
    records = issue_records(result, 7)
    keep = [record for record in records if _has_size_s(record) and _has_rounds(record, ROUNDS)]
    assert keep, f"Running #7 must keep size s and 6 rounds visible: {json.dumps(records)[:3000]}"
    measured = [record for record in keep if any(_line_counts(record))
                or any(LINES_KEY.search(path[-1]) for path, _ in leaves(record) if path)]
    assert measured, ("The joined record for #7 must carry PR line-count fields marked unknown: "
                      + json.dumps(keep)[:3000])
    for record in measured:
        added, deleted = _line_counts(record)
        assert 0 not in added and 0 not in deleted, "A missing PR measurement must not read as zero lines"
        unknown = (added and deleted and all(value is None or value == "unknown" for value in added + deleted)) or any(
            value is None or value == "unknown"
            for path, value in leaves(record) if path and LINES_KEY.search(path[-1]))
        assert unknown, "Line counts without a PR measurement must be explicitly unknown (null): " + json.dumps(record)


def test_missing_pr_measurement_is_unknown_not_zero_in_human(tmp_path, monkeypatch, capsys):
    from test_orch_facts import facts

    case = recorded_loop(tmp_path, monkeypatch)
    human = facts(case, capsys, human=True)
    lines = [line for line in human.splitlines()
             if re.search(r"#7\b", line) and re.search(r"(?i)\bsize\W{0,3}s\b", line)
             and re.search(rf"(?i)(?<![\d.]){ROUNDS}(?![\d.])\s*(review\s*)?rounds?|rounds?\W{{0,3}}{ROUNDS}\b", line)]
    assert lines, "The human facts must keep running #7's size s and 6 rounds visible:\n" + human
    line = lines[0]
    unknown = (r"(?i)(lines?|additions?|deletions?|added|deleted|\+/-|diff)\W{0,3}(\?|unknown)"
               r"|(\?|unknown)\W{0,3}(lines?|additions?|deletions?|added|deleted)")
    assert re.search(unknown, line.split("#7", 1)[1]), \
        "Unmeasured PR line counts must be shown as unknown on #7's line: " + line
    assert not re.search(r"(?i)[+-]0\b|(?<![\d.])0\s*(additions?|added|deletions?|deleted|lines?)\b", line), \
        "Unmeasured PR line counts must not be shown as zero: " + line
