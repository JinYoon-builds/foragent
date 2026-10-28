"""Acceptance test for lane-width-by-overlap. Locked by Foragent; do not edit."""

from dataclasses import replace

import pytest

from foragent.capacity import write_state
from foragent.scheduler import tick
from test_lane_clone import _lane_clone_issue
from test_scheduler import ctx  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


def _wide_lane(context, budget):
    context.config = replace(
        context.config, max_parallel=None, skill_libraries=[],
        lanes={"orch": {"paths": ["src/foragent/**"]}},
    )
    context.start_runs = True
    write_state(context.store, budget, context.now())
    starts = []

    def launch(number=None, *, kind="issue", lane=None):
        if kind != "issue":  # #189: lanes may dream; this test counts issue starts only.
            return None
        starts.append(number)
        return 7000 + number  # ctx records these in its fake live process table.

    context.launcher = launch
    return starts


def _finish(context, number):
    context.store.finish_run(number, outcome="merged")
    context.tracker.close_issue(number)
    context.tracker.remove_labels(number, ["running", "ready"])
    context.process_table.exit(7000 + number)


@pytest.mark.parametrize("budget,expected", [(5, [1, 2, 3, 4]), (3, [1, 2, 3])])
def test_lane_width_fills_machine_budget_with_disjoint_files_in_one_tick(ctx, budget, expected):
    starts = _wide_lane(ctx, budget)
    for number in range(1, 5):
        _lane_clone_issue(ctx, number, [f"src/foragent/file{number}.py"])
    assert not (ctx.store.root / "plan.json").exists()

    report = tick(ctx)

    assert report.skipped is None, report
    assert starts == expected, report.actions
    assert {(run.number, run.lane, run.pid) for run in ctx.store.active_runs()} == {
        (number, "orch", 7000 + number) for number in expected
    }
    assert all("ready" in ctx.tracker.get_issue(n).labels for n in set(range(1, 5)) - set(expected))


@pytest.mark.parametrize("conflicting_run", [1, 2, 3])
def test_lane_width_checks_every_running_issue_until_its_conflict_finishes(ctx, conflicting_run):
    starts = _wide_lane(ctx, 5)
    object.__setattr__(ctx.config, "overlap_per_file", 1)  # Main ignores the not-yet-declared Config field.
    for number in range(1, 4):
        _lane_clone_issue(ctx, number, [f"src/foragent/file{number}.py"], running=True)
        ctx.store.update_run(number, pid=7000 + number)
    _lane_clone_issue(ctx, 4, [f"src/foragent/file{conflicting_run}*.py"])
    _lane_clone_issue(ctx, 5, ["src/foragent/disjoint.py"])

    report = tick(ctx)
    assert report.skipped is None, report
    assert starts == [5], report.actions
    assert {run.number for run in ctx.store.active_runs()} == {1, 2, 3, 5}
    assert "ready" in ctx.tracker.get_issue(4).labels
    assert "running" not in ctx.tracker.get_issue(4).labels

    unrelated = next(number for number in range(1, 4) if number != conflicting_run)
    _finish(ctx, unrelated)
    report = tick(ctx)
    assert report.skipped is None, report
    assert starts == [5], report.actions
    assert ctx.store.get_run(4) is None

    _finish(ctx, conflicting_run)
    report = tick(ctx)
    assert report.skipped is None, report
    assert starts == [5, 4], report.actions
    assert {run.number for run in ctx.store.active_runs()} == ({1, 2, 3, 4, 5} - {unrelated, conflicting_run})


@pytest.mark.parametrize("files", [None, []], ids=["undeclared-files", "empty-files"])
def test_lane_width_empty_files_wait_for_and_exclusively_occupy_the_lane(ctx, files):
    starts = _wide_lane(ctx, 5)
    for number in (1, 2, 4):
        _lane_clone_issue(ctx, number, [f"src/foragent/file{number}.py"])
    _lane_clone_issue(ctx, 3, files)

    report = tick(ctx)
    assert report.skipped is None, report
    assert starts == [1, 2, 4], report.actions
    assert "ready" in ctx.tracker.get_issue(3).labels
    assert "running" not in ctx.tracker.get_issue(3).labels

    for number in (1, 2, 4):
        _finish(ctx, number)
    _lane_clone_issue(ctx, 5, ["src/foragent/file5.py"])
    report = tick(ctx)
    assert report.skipped is None, report
    assert starts == [1, 2, 4, 3], report.actions
    assert {run.number for run in ctx.store.active_runs()} == {3}
    assert "ready" in ctx.tracker.get_issue(5).labels

    _finish(ctx, 3)
    report = tick(ctx)
    assert report.skipped is None, report
    assert starts == [1, 2, 4, 3, 5], report.actions
    assert {run.number for run in ctx.store.active_runs()} == {5}
