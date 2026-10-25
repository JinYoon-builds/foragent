"""Acceptance test for lane-clone. Locked by Foragent; do not edit."""

import pytest

from test_scheduler import ctx
from test_skills_autosync import isolated_home


def _lane_clone_case(context):
    from dataclasses import replace

    context.config = replace(context.config, max_parallel=4, skill_libraries=[],
                             lanes={"orch": {"paths": ["src/foragent/**"]}})
    context.start_runs = True
    starts = []

    def launch(number):
        starts.append(number)
        return 7000 + number

    context.launcher = launch
    return starts


def _lane_clone_issue(context, number, files, *, running=False):
    from test_scheduler import BODY, approve

    # D69's expected-file metadata uses a Markdown list, like the other body sections.
    body = BODY
    if files is not None:
        body += "\n## Files\n" + "".join(f"- {path}\n" for path in files)
    approve(context, number, body=body, labels=["lane:orch", "running" if running else "ready"])
    if running:
        context.store.start_run(number, branch=f"foragent/issue-{number}",
                                worktree=context.store.worktree_path(number), lane="orch")


def test_lane_clone_starts_disjoint_issue_and_keeps_overlapping_issue_waiting(ctx):
    from foragent.scheduler import tick

    starts = _lane_clone_case(ctx)
    object.__setattr__(ctx.config, "overlap_per_file", 1)  # Main ignores the not-yet-declared Config field.
    _lane_clone_issue(ctx, 1, ["src/foragent/runner.py"], running=True)
    _lane_clone_issue(ctx, 2, ["src/foragent/state.py"])
    _lane_clone_issue(ctx, 3, ["src/foragent/runner.py"])
    report = tick(ctx)
    assert report.skipped is None
    assert starts == [2], report.actions
    assert {(run.number, run.lane) for run in ctx.store.active_runs()} == {(1, "orch"), (2, "orch")}
    assert "ready" in ctx.tracker.get_issue(3).labels
    assert "running" not in ctx.tracker.get_issue(3).labels


def test_lane_clone_never_adds_a_third_worker_even_with_capacity_and_backlog(ctx):
    from foragent.capacity import write_state
    from foragent.scheduler import tick

    starts = _lane_clone_case(ctx)
    write_state(ctx.store, 4, ctx.now())
    _lane_clone_issue(ctx, 1, ["src/foragent/runner.py"], running=True)
    _lane_clone_issue(ctx, 2, ["src/foragent/state.py"], running=True)
    _lane_clone_issue(ctx, 3, ["src/foragent/order.py"])
    _lane_clone_issue(ctx, 4, ["src/foragent/watch.py"])
    report = tick(ctx)
    assert starts == [3, 4], report.actions
    assert {run.number for run in ctx.store.active_runs()} == {1, 2, 3, 4}
    assert all("running" in ctx.tracker.get_issue(number).labels for number in (3, 4))


@pytest.mark.parametrize("reason", ["open-blocker", "empty-files", "unknown-files", "short-queue"])
def test_lane_clone_requires_unblocked_known_files_and_two_waiting_issues(ctx, reason):
    from foragent.scheduler import tick

    starts = _lane_clone_case(ctx)
    object.__setattr__(ctx.config, "overlap_per_file", 1)
    _lane_clone_issue(ctx, 1, ["src/foragent/runner.py"], running=True)
    files = [] if reason == "empty-files" else None if reason == "unknown-files" else ["src/foragent/state.py"]
    _lane_clone_issue(ctx, 2, files)
    if reason != "short-queue":
        # Keep another issue waiting, but it cannot safely clone the running worker.
        _lane_clone_issue(ctx, 3, ["src/foragent/runner.py"])
    if reason == "open-blocker":
        ctx.tracker.add_blocked_by(2, 1)
    report = tick(ctx)
    expected = [2] if reason == "short-queue" else []
    assert starts == expected, report.actions
    assert {run.number for run in ctx.store.active_runs()} == {1, *expected}
    assert ("running" in ctx.tracker.get_issue(2).labels) == (reason == "short-queue")
    assert not ctx.tracker.get_issue(2).labels & {"blocked", "needs-human"}


@pytest.mark.parametrize("running_files,candidate_files", [
    (["src/foragent/orch*.py"], ["src/foragent/orch.py"]),
    (["src/foragent/orch.py"], ["src/foragent/orch*.py"]),
])
def test_lane_clone_glob_overlap_is_symmetric_and_skips_to_disjoint_work(ctx, running_files, candidate_files):
    from foragent.scheduler import tick

    starts = _lane_clone_case(ctx)
    object.__setattr__(ctx.config, "overlap_per_file", 1)
    _lane_clone_issue(ctx, 1, running_files, running=True)
    _lane_clone_issue(ctx, 2, candidate_files)
    _lane_clone_issue(ctx, 3, ["src/foragent/state.py"])
    report = tick(ctx)
    assert starts == [3], report.actions
    assert {run.number for run in ctx.store.active_runs()} == {1, 3}
    assert "ready" in ctx.tracker.get_issue(2).labels
    assert "running" not in ctx.tracker.get_issue(2).labels


def test_lane_clone_respects_global_capacity_even_for_disjoint_work(ctx):
    from dataclasses import replace

    from foragent.scheduler import tick

    starts = _lane_clone_case(ctx)
    ctx.config = replace(ctx.config, max_parallel=3, lanes={
        **ctx.config.lanes, "api": {"paths": ["api/**"]}, "view": {"paths": ["view/**"]},
    })
    _lane_clone_issue(ctx, 1, ["src/foragent/runner.py"], running=True)
    _lane_clone_issue(ctx, 2, ["src/foragent/state.py"])
    _lane_clone_issue(ctx, 3, ["src/foragent/order.py"])
    from test_scheduler import BODY, approve

    for number, lane in ((4, "api"), (5, "view")):
        approve(ctx, number, body=BODY, labels=[f"lane:{lane}", "running"])
        ctx.store.start_run(number, branch=f"foragent/issue-{number}",
                            worktree=ctx.store.worktree_path(number), lane=lane)
    report = tick(ctx)
    assert starts == [], report.actions
    assert {run.number for run in ctx.store.active_runs()} == {1, 4, 5}
