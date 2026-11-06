"""Wide-first holds use committed measurements; declared Files still decide width."""

from test_scheduler import ctx  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_wide_issues_merge_first import (
    NARROW,
    WIDE,
    _case,
    _files,
    _issue,
    _tick,
    _waiting,
)


MESSAGES_GLOB = "src/foragent/messages/watch*.py"
NARROW_FILE = "src/foragent/messages/watch_scene.py"


def _measured_wide_run(context, effective_files):
    starts = _case(context)
    declared_files = [*_files(10), MESSAGES_GLOB]
    _issue(context, WIDE, declared_files)
    _tick(context)
    assert starts == [WIDE]
    # The fake launcher has no worktree to remeasure; seed its committed snapshot.
    context.store.update_run(
        WIDE,
        declared_files=declared_files,
        files_committed=True,
        effective_files=effective_files,
    )
    _issue(context, NARROW, [MESSAGES_GLOB], lane="view")
    return starts


def test_narrow_issue_is_not_held_on_a_declared_glob_the_wide_run_did_not_touch(ctx):
    starts = _measured_wide_run(ctx, _files(2))

    report = _tick(ctx)

    assert starts == [WIDE, NARROW], report.actions
    assert "running" in ctx.tracker.get_issue(NARROW).labels
    assert {run.number for run in ctx.store.active_runs()} == {WIDE, NARROW}
