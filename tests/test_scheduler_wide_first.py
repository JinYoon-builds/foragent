"""Boundaries of the wide-issues-merge-first rule (#2353) beyond the locked acceptance file."""

from dataclasses import replace

from foragent import overlap_wide
from foragent.capacity import write_state
from foragent.scheduler import tick
from helpers.home import isolated_home  # noqa: F401
from test_scheduler import BODY, approve, ctx  # noqa: F401


def _case(context):
    context.config = replace(context.config, max_parallel=None, skill_libraries=[], overlap_per_file=2,
                             lanes={"orch": {"paths": ["src/foragent/**"]}})
    context.start_runs = True
    write_state(context.store, 5, context.now())
    starts = []

    def launch(number=None, *, kind="issue", lane=None):
        if kind != "issue":
            return None
        starts.append(number)
        return 7000 + number

    context.launcher = launch
    return starts


def _files(count):
    return [f"src/foragent/wide{index}.py" for index in range(count)]


def _issue(context, number, files):
    body = BODY + "\n## Files\n" + "".join(f"- {path}\n" for path in files)
    approve(context, number, body=body, labels=["lane:orch", "ready"])


def _tick(context):
    report = tick(context)
    assert report.skipped is None, report
    return report


def _waiting(context, number):
    labels = context.tracker.get_issue(number).labels
    assert "ready" in labels and "running" not in labels, labels
    assert context.store.get_run(number) is None


def test_between_two_wide_issues_the_wider_one_goes_first(ctx):
    starts = _case(ctx)
    _issue(ctx, 1, _files(11))
    _issue(ctx, 2, _files(12))
    _tick(ctx)
    assert starts == [2]
    _waiting(ctx, 1)


def test_between_two_equally_wide_issues_the_older_one_goes_first(ctx):
    starts = _case(ctx)
    _issue(ctx, 2, _files(11))
    _issue(ctx, 1, _files(11))
    _tick(ctx)
    assert starts == [1]
    _waiting(ctx, 2)


def test_the_configured_threshold_decides_what_is_wide(ctx, monkeypatch):
    monkeypatch.setattr(overlap_wide, "max_files", lambda config: 3)
    starts = _case(ctx)
    _issue(ctx, 1, _files(4))
    _issue(ctx, 2, ["src/foragent/wide3.py"])
    _tick(ctx)
    assert starts == [1]
    _waiting(ctx, 2)


def test_threshold_reads_the_split_wide_issues_setting():
    class Config:
        issue_max_files = 7

    assert overlap_wide.max_files(Config()) == 7
    assert overlap_wide.max_files(object()) == 10
