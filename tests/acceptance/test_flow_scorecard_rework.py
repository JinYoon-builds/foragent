"""Acceptance test for flow-scorecard-rework. Locked by Foragent; do not edit.

A real run goes through the runner with scripted implementer and reviewer
replies. Each reply moves the injected clock, so phase minutes are known. The
scorecard is then read through the CLI. Expected minutes are hand-calculated.
"""

from datetime import timedelta
import os
import re

import pytest

from test_local_truth import local_cli, record_gh
from test_orch_scorecard import scorecard_case
from test_scorecard_flow import NOW, _read
from test_skills_autosync import isolated_home  # noqa: F401


FIVE = {"work", "concurrency_cost", "machine_limit", "ordering", "waste"}
FAIL = "VERDICT: FAIL\n- feature.txt:1 — The feature text is wrong.\n"
PASS = "VERDICT: PASS"
CI_LOG = "FAILED tests/test_feature.py::test_feature - AssertionError\n"


@pytest.fixture
def case(git_history, tmp_path, monkeypatch):
    from foragent import capacity

    case = scorecard_case(git_history, tmp_path, monkeypatch)
    case.files_root = tmp_path
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.25))
    monkeypatch.setattr(os, "getloadavg", lambda: (0.25, 0.25, 0.25))
    case.monkeypatch = monkeypatch
    return case


def taking(case, minutes, reply):
    """An executor step that answers ``reply`` and takes ``minutes`` of clock time."""
    def step(kwargs):
        message = reply(kwargs) if callable(reply) else reply
        case.clock[0] += timedelta(minutes=minutes)
        return message
    return step


def run_once(case, implementer, reviewer, *, outcome="merged"):
    """Approve issue 1 at T, start its run at T+2 min, and run it to ``outcome``."""
    from conftest import ACCEPTANCE
    from foragent.issue import body_hash
    from foragent.runner import run_issue

    approved = NOW - timedelta(hours=2)
    case.clock[0] = approved
    body = case.tracker.get_issue(1).body
    case.store.save_approval(1, body=body, body_sha=body_hash(body),
                             acceptance_blobs=case.git.blob_shas("origin/main", [ACCEPTANCE]),
                             approved_by="human", approved_at=approved.isoformat())
    case.store.update_local_issue(1, title="Implement feature", body=body,
                                  labels=["approved", "running"], state="open", blocked_by=[])
    # Nothing is recorded about these two minutes: they must stay unknown.
    case.clock[0] = approved + timedelta(minutes=2)
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))
    case.implementer.steps = implementer
    case.reviewer.steps = reviewer
    assert run_issue(1, case.ctx) == outcome


def card(case, capsys, *, human=False):
    """Read the scorecard through the CLI, with GitHub calls and writes forbidden."""
    if not hasattr(case, "main"):
        case.gh_calls = record_gh(case.monkeypatch)
        case.main = local_cli(case, case.monkeypatch)
    case.clock[0] = NOW
    return _read(case, capsys, human=human)


def split(value):
    parts = value["parts"]
    assert "rework" in parts, "Rework must be its own part of approval to merge"
    assert FIVE | {"unknown"} <= set(parts), "The five earlier parts and unknown stay"
    assert all(type(minutes) in (int, float) and minutes >= 0 for minutes in parts.values())
    assert sum(parts.values()) == pytest.approx(value["total_minutes"]), "The parts add up to the total"
    return parts


def windows(payload):
    """Today's parts; the earlier days' window has the same parts, all zero here."""
    today = split(payload["window"]["today"]["approval_to_merge"])
    before = split(payload["window"]["previous"]["approval_to_merge"])
    assert before["rework"] == 0
    return today


def shown(text):
    """Today's split line in the human output: minutes after each part name."""
    lines = [line for line in text.lower().splitlines() if "rework" in line and "unknown" in line]
    assert lines, f"The human scorecard shows rework next to the other parts:\n{text}"
    found = {name: float(number) for name, number in
             re.findall(r"\b(work|rework|unknown)\b[^0-9,;]*?(\d+(?:\.\d+)?)", lines[0])}
    assert {"work", "rework", "unknown"} <= found.keys(), lines[0]
    return found


def test_review_fail_then_second_round_is_rework_not_unknown(case, capsys):
    # 10 min first build, 5 min review that fails, 7 min second build, 3 min passing review.
    run_once(case,
             [taking(case, 10, case.implement), taking(case, 7, case.implement)],
             [taking(case, 5, FAIL), taking(case, 3, PASS)])
    payload = card(case, capsys)
    assert payload["window"]["today"]["merges"] == 1
    value = payload["window"]["today"]["approval_to_merge"]
    parts = windows(payload)
    assert value["total_minutes"] == pytest.approx(27)
    assert parts["rework"] == pytest.approx(10), "The round after the FAIL is rework"
    assert parts["work"] == pytest.approx(15), "The first build and review stay work"
    assert parts["unknown"] == pytest.approx(2), "Only the unrecorded wait before the run stays unknown"
    assert sum(parts[name] for name in FIVE - {"work"}) == 0
    assert shown(card(case, capsys, human=True)) == {"work": 15, "rework": 10, "unknown": 2}


def test_ci_failure_then_second_round_is_rework_not_unknown(case, capsys):
    case.tracker.set_pr_checks(2, "fail")
    case.tracker.set_pr_failed_logs(2, CI_LOG)

    def fix(kwargs):
        case.implement(kwargs)
        case.tracker.set_pr_checks(2, "pass")
        return "Fixed CI"

    # 10 min build, 5 min passing review, CI fails, 7 min fix, 3 min passing review.
    run_once(case,
             [taking(case, 10, case.implement), taking(case, 7, fix)],
             [taking(case, 5, PASS), taking(case, 3, PASS)])
    payload = card(case, capsys)
    value = payload["window"]["today"]["approval_to_merge"]
    parts = windows(payload)
    assert value["total_minutes"] == pytest.approx(27)
    assert parts["rework"] == pytest.approx(10), "The round after the CI failure is rework"
    assert parts["work"] == pytest.approx(15)
    assert parts["unknown"] == pytest.approx(2)
    assert sum(parts[name] for name in FIVE - {"work"}) == 0


def test_run_without_a_failure_has_zero_rework_and_keeps_unknown(case, capsys):
    run_once(case, [taking(case, 10, case.implement)], [taking(case, 5, PASS)])
    payload = card(case, capsys)
    value = payload["window"]["today"]["approval_to_merge"]
    parts = windows(payload)
    assert value["total_minutes"] == pytest.approx(17)
    assert parts["rework"] == 0, "No FAIL and no CI failure: nothing is rework"
    assert parts["work"] == pytest.approx(15)
    assert parts["unknown"] == pytest.approx(2), "Missing evidence is never counted as rework or zero"
    assert shown(card(case, capsys, human=True)) == {"work": 15, "rework": 0, "unknown": 2}


def test_second_round_without_a_recorded_failure_is_not_rework(case, capsys):
    from foragent.executors import ExecResult
    from foragent.runner import run_issue

    scripted = case.implementer.run

    def run(**kwargs):
        if case.implementer.calls:
            return scripted(**kwargs)
        # The first build hits the executor's usage limit after 10 min: no review, no CI.
        case.implementer.calls.append(kwargs)
        case.clock[0] += timedelta(minutes=10)
        return ExecResult(1, False, "", kwargs["log_path"], error_kind="limit")

    case.implementer.run = run
    run_once(case, [taking(case, 7, case.implement)], [taking(case, 3, PASS)], outcome="limit-wait")
    # The next run starts after the limit resets and builds again with no failure on record.
    case.clock[0] += timedelta(minutes=61)
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))
    assert run_issue(1, case.ctx) == "merged"
    payload = card(case, capsys)
    value = payload["window"]["today"]["approval_to_merge"]
    parts = windows(payload)
    assert payload["window"]["today"]["merges"] == 1
    assert value["total_minutes"] == pytest.approx(83)
    assert parts["rework"] == 0, "A second build that no FAIL or CI failure caused is not rework"
    assert parts["work"] == pytest.approx(13)
