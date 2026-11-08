"""Boundaries of wide-first holds that read the wide run's committed measurement (#2413)."""

from dataclasses import replace

from foragent import overlap_wide
from foragent.capacity import write_state
from foragent.issue import parse_body
from foragent.scheduler import tick
from helpers.home import isolated_home  # noqa: F401
from test_scheduler import BODY, approve, ctx  # noqa: F401

WIDE = 1
NARROW = 2

GLOB = "src/foragent/messages/watch*.py"
TOUCHED = "src/foragent/messages/watch_scene.py"


def _files(count):
    return [f"src/foragent/wide{index}.py" for index in range(count)]


def _case(context):
    context.config = replace(
        context.config, max_parallel=None, skill_libraries=[], overlap_per_file=2,
        lanes={lane: {"paths": ["src/foragent/**"]} for lane in ("orch", "view")})
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


def _issue(context, number, files, *, lane="orch"):
    body = BODY + "\n## Files\n" + "".join(f"- {path}\n" for path in files)
    approve(context, number, body=body, labels=[f"lane:{lane}", "ready"])


def _tick(context):
    report = tick(context)
    assert report.skipped is None, report
    return report


def _waiting(context, number):
    labels = context.tracker.get_issue(number).labels
    assert "ready" in labels and "running" not in labels, labels
    assert context.store.get_run(number) is None


def _wide(context, **measurement):
    starts = _case(context)
    declared = [*_files(10), GLOB]
    _issue(context, WIDE, declared)
    _tick(context)
    assert starts == [WIDE]
    context.store.update_run(WIDE, declared_files=declared, **measurement)
    _issue(context, NARROW, [GLOB], lane="view")
    return starts


def _narrow():
    return parse_body(BODY + f"\n## Files\n- {GLOB}\n")


def test_committed_measurement_on_the_narrow_file_still_holds_even_when_short(ctx):
    # One measured file: width still comes from the eleven declared Files.
    starts = _wide(ctx, files_committed=True, effective_files=[TOUCHED])
    _tick(ctx)
    assert starts == [WIDE]
    _waiting(ctx, NARROW)


def test_uncommitted_measurement_keeps_the_declared_hold(ctx):
    starts = _wide(ctx, files_committed=False, effective_files=_files(2))
    _tick(ctx)
    assert starts == [WIDE]
    _waiting(ctx, NARROW)


def test_run_bound_to_an_older_approval_keeps_the_declared_hold(ctx):
    starts = _wide(ctx, files_committed=True, effective_files=_files(2),
                   approval_body_sha="older-approval")
    _tick(ctx)
    assert starts == [WIDE]
    _waiting(ctx, NARROW)


def _merge_queued(context, effective_files):
    _wide(context, files_committed=True, effective_files=effective_files)
    context.store.finish_run(WIDE, outcome="merge-queued")
    held = overlap_wide.holds(context, NARROW, _narrow())
    return [(hold["file"], [peer["issue"] for peer in hold["peers"]]) for hold in held]


def test_finished_merge_queued_run_releases_an_untouched_glob(ctx):
    assert _merge_queued(ctx, _files(2)) == []


def test_finished_merge_queued_run_holds_a_touched_file(ctx):
    assert _merge_queued(ctx, [TOUCHED]) == [(GLOB, [WIDE])]
