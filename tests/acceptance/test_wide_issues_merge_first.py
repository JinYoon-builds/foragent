"""Acceptance test for wide-issues-merge-first.

A wide approved issue (Files above the split-wide-issues threshold, default 10 files)
goes first: a narrow issue that needs one of its files does not start before the wide
one has merged, unless the global priority list puts the narrow issue first. Issues
that share no file with the wide issue start as today.
"""

from dataclasses import replace

import pytest

from foragent import orch_queue
from foragent.capacity import write_state
from foragent.scheduler import tick
from test_scheduler import BODY, approve, ctx  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401

WIDE = 1
NARROW = 2
DISJOINT = 3
SHARED = "src/foragent/wide3.py"


def _files(count):
    return [f"src/foragent/wide{index}.py" for index in range(count)]


def _case(context, budget=5):
    context.config = replace(
        context.config, max_parallel=None, skill_libraries=[], overlap_per_file=2,
        lanes={lane: {"paths": ["src/foragent/**"]} for lane in ("orch", "view")},
    )
    context.start_runs = True
    write_state(context.store, budget, context.now())
    starts = []

    def launch(number=None, *, kind="issue", lane=None):
        if kind != "issue":  # Lanes may dream; only issue starts count here.
            return None
        starts.append(number)
        return 7000 + number  # ctx records these in its fake live process table.

    context.launcher = launch
    return starts


def _issue(context, number, files, *, lane="orch"):
    body = BODY + "\n## Files\n" + "".join(f"- {path}\n" for path in files)
    approve(context, number, body=body, labels=[f"lane:{lane}", "ready"])


def _end_run(context, number, outcome, *, labels=(), close=False):
    context.store.finish_run(number, outcome=outcome)
    context.tracker.remove_labels(number, ["running", "ready"])
    if labels:
        context.tracker.add_labels(number, list(labels))
    if close:
        context.tracker.close_issue(number)
    context.process_table.exit(7000 + number)


def _waiting(context, number):
    labels = context.tracker.get_issue(number).labels
    assert "ready" in labels and "running" not in labels, labels
    assert context.store.get_run(number) is None


CONTROL_WIDE = 4
CONTROL_NARROW = 5
SCENARIO = (WIDE, NARROW, DISJOINT)


def _add_control(context):
    """A second, unrelated wide/narrow pair: the rule must hold it in every scenario."""
    _issue(context, CONTROL_WIDE, [f"src/foragent/far{index}.py" for index in range(11)], lane="view")
    _issue(context, CONTROL_NARROW, ["src/foragent/far3.py"], lane="view")


def _scenario(starts):
    return [number for number in starts if number in SCENARIO]


def _assert_control(context, starts, report):
    assert starts.count(CONTROL_WIDE) == 1, report.actions
    assert CONTROL_NARROW not in starts, report.actions
    _waiting(context, CONTROL_NARROW)


def _tick(context):
    report = tick(context)
    assert report.skipped is None, report
    return report


@pytest.mark.parametrize("narrow_lane", ["orch", "view"], ids=["same-lane", "other-lane"])
@pytest.mark.parametrize("wide_number,narrow_number", [(1, 2), (2, 1)], ids=["wide-older", "narrow-older"])
def test_narrow_issue_waits_until_the_wide_issue_has_merged(ctx, narrow_lane, wide_number, narrow_number):
    starts = _case(ctx)
    _issue(ctx, wide_number, _files(11))
    _issue(ctx, narrow_number, [SHARED], lane=narrow_lane)

    report = _tick(ctx)
    assert starts == [wide_number], report.actions
    _waiting(ctx, narrow_number)

    # Still running: the narrow issue keeps waiting, tick after tick.
    for _ in range(2):
        report = _tick(ctx)
        assert starts == [wide_number], report.actions
        _waiting(ctx, narrow_number)

    _end_run(ctx, wide_number, "merged", close=True)
    report = _tick(ctx)
    assert starts == [wide_number, narrow_number], report.actions
    assert {run.number for run in ctx.store.active_runs()} == {narrow_number}


def test_wide_issue_waiting_for_its_merge_still_holds_the_narrow_issue(ctx):
    starts = _case(ctx)
    _issue(ctx, WIDE, _files(11))
    _issue(ctx, NARROW, [SHARED])
    _tick(ctx)
    assert starts == [WIDE]

    # The run handed its PR to the merge owner; the issue is open and not merged yet.
    _end_run(ctx, WIDE, "merge-queued")
    report = _tick(ctx)
    assert NARROW not in starts, report.actions
    _waiting(ctx, NARROW)


def test_issues_sharing_no_file_with_the_wide_issue_start_as_today(ctx):
    starts = _case(ctx)
    _issue(ctx, WIDE, _files(11))
    _issue(ctx, NARROW, [SHARED])
    _issue(ctx, DISJOINT, ["src/foragent/elsewhere.py"], lane="view")

    report = _tick(ctx)
    assert sorted(starts) == [WIDE, DISJOINT], report.actions
    assert len(starts) == len(set(starts)), report.actions
    _waiting(ctx, NARROW)


def test_no_wide_issue_means_overlapping_issues_start_as_today(ctx):
    starts = _case(ctx)
    _issue(ctx, WIDE, _files(4))
    _issue(ctx, NARROW, [SHARED])
    _issue(ctx, DISJOINT, ["src/foragent/elsewhere.py"])
    _add_control(ctx)

    report = _tick(ctx)
    assert sorted(_scenario(starts)) == [WIDE, NARROW, DISJOINT], report.actions
    _assert_control(ctx, starts, report)


@pytest.mark.parametrize("count,holds", [(10, False), (11, True)], ids=["at-threshold", "above-threshold"])
def test_default_threshold_is_ten_files(ctx, count, holds):
    starts = _case(ctx)
    _issue(ctx, WIDE, _files(count))
    _issue(ctx, NARROW, [SHARED])
    _add_control(ctx)

    report = _tick(ctx)
    if holds:
        assert _scenario(starts) == [WIDE], report.actions
        _waiting(ctx, NARROW)
    else:
        assert sorted(_scenario(starts)) == [WIDE, NARROW], report.actions
    _assert_control(ctx, starts, report)


def test_unapproved_wide_issue_holds_nothing(ctx):
    starts = _case(ctx)
    body = BODY + "\n## Files\n" + "".join(f"- {path}\n" for path in _files(11))
    ctx.tracker.add_issue(WIDE, "Issue 1", body, labels=["lane:orch"])
    _issue(ctx, NARROW, [SHARED])
    _add_control(ctx)

    report = _tick(ctx)
    assert _scenario(starts) == [NARROW], report.actions
    _assert_control(ctx, starts, report)


@pytest.mark.parametrize("order,narrow_first", [
    ([NARROW], True),
    ([NARROW, WIDE], True),
    ([WIDE, NARROW], False),
    ([WIDE], False),
], ids=["only-narrow", "narrow-then-wide", "wide-then-narrow", "only-wide"])
def test_global_priority_list_decides_against_the_wide_rule(ctx, order, narrow_first):
    starts = _case(ctx)
    _issue(ctx, WIDE, _files(11))
    _issue(ctx, NARROW, [SHARED], lane="view")
    _add_control(ctx)
    orch_queue.set_global(ctx.store, order)

    report = _tick(ctx)
    if narrow_first:
        assert NARROW in starts, report.actions
        assert starts.count(NARROW) == 1, report.actions
        assert ctx.store.get_run(NARROW) is not None
    else:
        assert _scenario(starts) == [WIDE], report.actions
        _waiting(ctx, NARROW)
    # Issues the list does not name keep the wide-first rule.
    _assert_control(ctx, starts, report)


@pytest.mark.parametrize("ending", ["blocked", "needs-human", "parked", "closed-unmerged"])
def test_narrow_issue_starts_once_the_running_wide_issue_stops_without_merging(ctx, ending):
    starts = _case(ctx)
    _issue(ctx, WIDE, _files(11))
    _issue(ctx, NARROW, [SHARED])
    report = _tick(ctx)
    assert starts == [WIDE], report.actions
    _waiting(ctx, NARROW)

    if ending == "blocked":
        _end_run(ctx, WIDE, "blocked", labels=["blocked"])
    elif ending == "needs-human":
        _end_run(ctx, WIDE, "blocked", labels=["needs-human"])
    elif ending == "parked":
        _end_run(ctx, WIDE, "killed", labels=["waiting-person"])
        ctx.tracker.remove_labels(WIDE, ["approved"])
    else:
        _end_run(ctx, WIDE, "killed", close=True)

    report = _tick(ctx)
    assert starts == [WIDE, NARROW], report.actions
    assert {run.number for run in ctx.store.active_runs()} == {NARROW}


@pytest.mark.parametrize("state", ["blocked", "needs-human", "open-prerequisite"])
def test_wide_issue_that_cannot_start_holds_no_narrow_issue(ctx, state):
    starts = _case(ctx)
    _issue(ctx, WIDE, _files(11))
    if state == "open-prerequisite":
        ctx.tracker.add_issue(9, "Prerequisite", BODY, labels=["lane:view"])
        ctx.tracker.add_blocked_by(WIDE, 9)
    else:
        ctx.tracker.add_labels(WIDE, [state])
    _issue(ctx, NARROW, [SHARED])
    _add_control(ctx)

    report = _tick(ctx)
    assert _scenario(starts) == [NARROW], report.actions
    _assert_control(ctx, starts, report)
