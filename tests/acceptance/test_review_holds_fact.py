"""Acceptance test for review-holds-fact. Locked by Foragent; do not edit.

The repeated_design_holds fact (architect r705) counts a held execution only
when the run ended on the reviewer hold, judged by the structured reason the
runner passes to flag(), never by matching the hold's text. CI-evidence and
harness holds (acceptance baseline unavailable, CI not started, test harness
changed, PR head changed) are not design holds.

The reviewer hold's reason is taken from a real runner hold (a reviewer that
keeps failing), so this file does not pin which Reason value the lane uses
for it. Observation stays process-free, as in test_orch_facts.
Break-it: counting every held row, matching hold text, counting a hold twice,
or dropping a reviewer hold must each fail a case below.
"""

from datetime import timedelta
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from foragent.executors import ExecResult
from foragent.runner import run_issue
from test_local_truth import record_gh
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_review_verdict_line import ordinary_case, response
from test_risk_map import offline  # noqa: F401
from test_role_signals import signal_case
from test_role_source import NOW, role_entry, tree_bytes, write_role_snapshot
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401

KIND = "repeated_design_holds"
ARCHITECT_KINDS = ["repeated_design_holds", "hot_overlap_files", "files_over_limit", "idle_lanes_with_backlog"]


def reviewer_hold_reason(git_history, tmp_path, monkeypatch):
    """The reason the real runner flags when the reviewer still finds defects."""
    from foragent.state import StateStore

    seen = []
    save_flag = StateStore.save_flag

    def spy(self, number, reason, **kwargs):
        seen.append(reason)
        return save_flag(self, number, reason, **kwargs)

    root = tmp_path / "runner"
    root.mkdir()
    case = ordinary_case(git_history, root, monkeypatch, "codex")
    case.reviewer.run = lambda **kwargs: response(
        kwargs, "VERDICT: FAIL\nfeature.txt:1 — the approved feature loses data")
    with monkeypatch.context() as patch:
        patch.setattr(StateStore, "save_flag", spy)
        assert run_issue(1, case.ctx) == "held"
    comments = "\n".join(comment.body for comment in case.tracker.list_comments(1))
    assert "Reviewer still found defects" in comments, "Fixture: the runner must end on the reviewer hold"
    assert seen, "Fixture: the reviewer hold must be flagged with a structured reason"
    return seen[-1]


def holds_case(git_history, tmp_path, monkeypatch):
    from foragent import resource_sample
    from foragent.config import load_config
    from foragent.tracker import FakeTracker

    reviewer = reviewer_hold_reason(git_history, tmp_path, monkeypatch)
    case = signal_case(tmp_path, monkeypatch)
    case.reviewer_reason = reviewer
    case.roles["architect"] = role_entry("architect", signals=[{"paths": [], "kinds": ARCHITECT_KINDS}])
    write_role_snapshot(case.root, case.roles)
    # Freeze the gate input; observations must not read host load.
    resource_sample.record(case.store, ("normal", 0.1), NOW)
    case.tracker, case.issues = FakeTracker(now=lambda: case.clock[0]), set()
    case.ctx = SimpleNamespace(store=case.store, tracker=case.tracker, now=lambda: case.clock[0],
                               config=load_config(case.root), repo="owner/repo")
    return case


def held(case, number, *, finished, reason, summary, details=None):
    """One execution that the runner ended by flagging ``reason`` and finishing as held."""
    from foragent.actions import flag

    if number not in case.issues:
        case.issues.add(number)
        case.tracker.add_issue(number, f"Issue {number}", "", labels=["approved", "running"])
    case.clock[0] = finished - timedelta(minutes=20)
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    case.clock[0] = finished
    flag(case.ctx, number, reason, summary, details)
    case.store.finish_run(number, outcome="held")
    case.clock[0] = NOW


def reviewer_held(case, number, *, finished, rounds=3):
    held(case, number, finished=finished, reason=case.reviewer_reason,
         summary=f"Reviewer still found defects after {rounds} rounds",
         details=f"feature.txt:1 — the approved feature loses data\n\nhttps://github.com/owner/repo/pull/{number + 100}")


def observe(case, monkeypatch, capsys, command):
    capsys.readouterr()
    before = tree_bytes(case.files_root)

    def forbidden(*args, **kwargs):
        pytest.fail("Facts and role observations must only read local records, never launch or signal a process")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", forbidden)
        patch.setattr(os, "kill", forbidden)
        patch.setattr(os, "killpg", forbidden)
        calls = record_gh(patch)
        code = cli_exit(case.main, ["orch", command, "--json", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"fa orch {command} --json must succeed from local records: {output.err.strip()}"
    assert calls == [], f"fa orch {command} must make no GitHub calls"
    assert tree_bytes(case.files_root) == before, "Observation must not write state"
    return json.loads(output.out)


def design_holds(case, monkeypatch, capsys):
    facts = observe(case, monkeypatch, capsys, "facts")
    numbers = facts.get("kind_numbers", {}).get(KIND, {})
    present = KIND in facts.get("kinds", [])
    assert present == bool(numbers), "Kind presence and its numbers must agree"
    return present, numbers.get("holds_by_issue", {})


def architect_met(case, monkeypatch, capsys):
    rows = observe(case, monkeypatch, capsys, "roles")["roles"]
    return rows["architect"]["signals"][0]["met"]


def test_reviewer_holds_count_once_each_and_fire_the_architect(git_history, tmp_path, monkeypatch, capsys):
    from foragent.reasons import Reason

    case = holds_case(git_history, tmp_path, monkeypatch)
    # #7: two reviewer holds (the text differs by round count) and one CI hold between them.
    reviewer_held(case, 7, finished=NOW - timedelta(days=3))
    held(case, 7, finished=NOW - timedelta(days=2), reason=Reason.CI_NOT_STARTED,
         summary="CI jobs did not start", details="runner queue empty")
    reviewer_held(case, 7, finished=NOW - timedelta(hours=5), rounds=2)
    # #8: one reviewer hold plus a PR-head-changed hold is not a repeated design hold.
    reviewer_held(case, 8, finished=NOW - timedelta(days=1))
    held(case, 8, finished=NOW - timedelta(hours=3), reason=Reason.RISK_HIGH,
         summary="PR head changed after verification", details="https://github.com/owner/repo/pull/108")
    present, holds = design_holds(case, monkeypatch, capsys)
    assert present and holds == {"#7": 2}, (
        f"Count only executions that ended on the reviewer hold, once each: {holds}")
    assert architect_met(case, monkeypatch, capsys) is True, (
        "Two reviewer holds of one issue in 7 days meet the architect's repeated_design_holds signal")


def test_one_reviewer_hold_and_a_baseline_hold_count_one_and_do_not_fire(
        git_history, tmp_path, monkeypatch, capsys):
    from foragent.reasons import Reason

    case = holds_case(git_history, tmp_path, monkeypatch)
    reviewer_held(case, 7, finished=NOW - timedelta(days=4))
    # The CI-evidence hold quotes the reviewer's words; text must never decide.
    held(case, 7, finished=NOW - timedelta(days=1), reason=Reason.ACCEPTANCE_BASELINE_UNAVAILABLE,
         summary="Acceptance comparison unavailable",
         details="Stale acceptance shard. Last run: Reviewer still found defects after 3 rounds")
    present, holds = design_holds(case, monkeypatch, capsys)
    assert not present and holds == {}, f"An acceptance-baseline-unavailable hold is not a design hold: {holds}"
    assert architect_met(case, monkeypatch, capsys) is False, "One reviewer hold must not call the architect"
    # The reviewer hold did count once: one more reviewer hold makes two, not three.
    reviewer_held(case, 7, finished=NOW - timedelta(hours=2))
    present, holds = design_holds(case, monkeypatch, capsys)
    assert present and holds == {"#7": 2}, f"The issue had exactly two reviewer holds: {holds}"
    assert architect_met(case, monkeypatch, capsys) is True


@pytest.mark.parametrize("reason,summary,details", (
    ("ACCEPTANCE_BASELINE_UNAVAILABLE", "Acceptance comparison unavailable",
     "missing acceptance shards: report.shard_index"),
    ("CI_NOT_STARTED", "CI jobs did not start", "no runner picked up the jobs"),
    ("TEST_HARNESS_CHANGED", "This PR changes test configuration outside ## Files",
     "pyproject.toml"),
    ("RISK_HIGH", "PR head changed after verification", "https://github.com/owner/repo/pull/109"),
), ids=("baseline-unavailable", "ci-not-started", "harness-changed", "pr-head-changed"))
def test_issue_with_only_ci_evidence_or_harness_holds_is_absent(
        git_history, tmp_path, monkeypatch, capsys, reason, summary, details):
    from foragent.reasons import Reason

    case = holds_case(git_history, tmp_path, monkeypatch)
    for days in (5, 3, 1):
        held(case, 9, finished=NOW - timedelta(days=days), reason=Reason[reason], summary=summary,
             details=f"{details}\nReviewer still found defects after 3 rounds (previous attempt)")
    present, holds = design_holds(case, monkeypatch, capsys)
    assert not present and "#9" not in holds, f"Non-review holds are never design holds: {holds}"
    assert architect_met(case, monkeypatch, capsys) is False
