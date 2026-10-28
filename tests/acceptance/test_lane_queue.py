"""Acceptance test for lane-queue. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
import os

import pytest

from foragent.scheduler import tick
from foragent.state import StateStore
from test_local_truth import local_cli, record_gh
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home


@pytest.fixture
def queue_case(git_history, tmp_path, monkeypatch, isolated_home):
    case = make_case(
        git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
        lanes={"orch": {"paths": ["src/foragent/**"]}},
    )
    case.store.finish_run(1, outcome="not-started")
    case.tracker.remove_labels(1, ["running", "approved"])
    case.tracker.close_issue(1)
    body = case.tracker.get_issue(1).body
    # Real approval events and acceptance blobs make every candidate startable.
    for number in (3, 5, 7):
        case.tracker.add_issue(number, f"Task {number}", body, labels=["ready", "lane:orch"])
        case.tracker.record_label(number, "approved", "human")

    case.main = local_cli(case, monkeypatch)
    # Each CLI invocation gets a fresh store; persistence cannot be an in-memory queue.
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kwargs: cls(case.store.root, now=lambda: case.clock[0])))
    case.gh_calls = record_gh(monkeypatch)
    case.tick_ctx = tick_context(case)
    case.launched = []

    def launch(number=None, *, kind="issue", lane=None):
        if kind != "issue":  # #189: lanes may dream; this test counts issue starts only.
            return None
        case.launched.append(number)
        return os.getpid()  # A second tick must see a live runner, not recover a crash.

    case.tick_ctx.launcher = launch
    return case


def queue_output(case, capsys, *arguments):
    argv = ["orch", "queue", *map(str, arguments)]
    code = cli_exit(case.main, argv)
    captured = capsys.readouterr()
    assert code == 0, f"fa {' '.join(argv)} must succeed (exit={code}):\n{captured.err}"
    return captured.out


def assert_queue(case, capsys, order):
    assert json.loads(queue_output(case, capsys, "orch", "--json")) == {
        "lane": "orch", "order": order,
    }


def assert_tick_starts(case, expected):
    report = tick(case.tick_ctx)
    assert report.skipped is None, report
    assert case.launched == expected, report.actions
    assert [(run.number, run.lane, run.pid) for run in case.store.active_runs()] == [
        (expected[-1], "orch", os.getpid()),
    ], report.actions


def finish_issue(case, number):
    case.store.finish_run(number, outcome="merged")
    case.tracker.close_issue(number)
    case.tracker.remove_labels(number, ["running", "ready"])
    # Permit the normal local-truth refresh to observe these direct tracker changes.
    case.clock[0] += timedelta(minutes=5, seconds=1)


def test_queue_cli_persists_replaces_and_clears_locally(queue_case, capsys):
    case = queue_case
    output = queue_output(case, capsys, "orch", 7, 3)
    assert "7" in output and "3" in output, output
    assert output.index("7") < output.index("3"), output
    assert_queue(case, capsys, [7, 3])

    queue_output(case, capsys, "orch", 5, 7)
    assert_queue(case, capsys, [5, 7])
    queue_output(case, capsys, "orch", "--clear")
    assert_queue(case, capsys, [])
    assert case.gh_calls == []
    # Clearing must also remove the preference used by the scheduler.
    assert_tick_starts(case, [3])


@pytest.mark.parametrize("order, expected", [
    ([7, 3], [7, 3, 5]),
    ([7, 5], [7, 5, 3]),
    ([7], [7, 3, 5]),
], ids=["7-before-3", "5-before-3", "unqueued-number-order"])
def test_tick_obeys_queue_before_unqueued_number_order(queue_case, capsys, order, expected):
    case = queue_case
    queue_output(case, capsys, "orch", *order)
    assert_queue(case, capsys, order)
    for index, number in enumerate(expected):
        assert_tick_starts(case, expected[:index + 1])
        if index == 0:
            # An occupied lane must keep its one live runner across another tick.
            assert_tick_starts(case, expected[:1])
        finish_issue(case, number)
    report = tick(case.tick_ctx)
    assert case.launched == expected and case.store.active_runs() == [], report.actions
    assert all(case.tracker.blocked_by(number) == [] for number in (3, 5, 7))
    assert case.gh_calls == []


def test_real_dependency_wins_until_blocker_closes_and_lane_is_free(queue_case, capsys):
    case = queue_case
    case.tracker.add_issue(11, "Real prerequisite")
    case.tracker.add_blocked_by(7, 11)
    queue_output(case, capsys, "orch", 7, 3)
    assert_tick_starts(case, [3])
    assert case.store.get_run(7) is None
    assert "running" not in case.tracker.get_issue(7).labels

    case.tracker.close_issue(11)
    case.clock[0] += timedelta(minutes=5, seconds=1)
    assert_tick_starts(case, [3])
    assert case.store.get_run(7) is None
    assert_queue(case, capsys, [7, 3])
    finish_issue(case, 3)
    assert_tick_starts(case, [3, 7])
    assert case.store.get_run(5) is None
    assert [(dep.number, dep.state) for dep in case.tracker.blocked_by(7)] == [(11, "closed")]
    assert case.gh_calls == []


def test_blocked_queue_head_steps_aside_and_keeps_its_priority(queue_case, capsys):
    case = queue_case
    case.tracker.remove_labels(3, ["ready"])
    case.tracker.add_labels(3, ["blocked"])
    case.tracker.comment(3, "<!-- foragent:blocked reason=spec-question -->\nWhich locale?")
    queue_output(case, capsys, "orch", 3, 7)
    assert_tick_starts(case, [7])
    assert case.store.get_run(3) is None
    assert "blocked" in case.tracker.get_issue(3).labels
    assert_queue(case, capsys, [3, 7])

    case.tracker.remove_labels(3, ["blocked"])
    case.tracker.add_labels(3, ["ready"])
    case.clock[0] += timedelta(minutes=5, seconds=1)
    assert_tick_starts(case, [7])
    assert case.store.get_run(3) is None
    finish_issue(case, 7)
    assert_tick_starts(case, [7, 3])
    assert case.store.get_run(5) is None
    assert_queue(case, capsys, [3, 7])
    assert all(case.tracker.blocked_by(number) == [] for number in (3, 5, 7))
    assert case.gh_calls == []


def test_queue_ignores_closed_and_unknown_numbers_without_github_calls(queue_case, capsys):
    case = queue_case
    case.tracker.add_issue(9, "Already finished", state="closed")
    queue_output(case, capsys, "orch", 9, 999, 7, 3)
    assert case.gh_calls == []
    assert_tick_starts(case, [7])
    assert case.store.get_run(9) is None and case.store.get_run(999) is None
    finish_issue(case, 7)
    assert_tick_starts(case, [7, 3])
    assert case.store.get_run(5) is None
    assert case.gh_calls == []
