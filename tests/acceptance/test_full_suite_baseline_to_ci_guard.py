"""Regression controls for full-suite-baseline-to-ci (2026-11-08); not a Done when file.

These pass on main and must keep passing:
- a run that selects only part of the suite keeps today's behaviour (no record that
  the comparison is left to PR CI, no local full baseline in CI mode);
- with merge_owner off, a whole-suite (workflow-changing) run still captures its
  local acceptance baseline;
- without CI (checks: local), the run still captures its local acceptance baseline;
- the merge owner never starts a local full baseline for the queued whole-suite PR.
"""

from dataclasses import replace

from acceptance.test_merge_path_real_settings import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support

import full_suite_ci_world as fs


def configure(case, **change):
    case.config = replace(case.config, **change)
    case.ctx.config = case.config


def test_partial_selection_keeps_todays_ci_comparison(proto, monkeypatch):
    case = fs.prepare(proto, monkeypatch, edit_workflow=False)

    outcome = fs.run(case)

    assert outcome == "merge-queued" and case.store.load_flag(fs.N) is None, fs.log(case)[-2000:]
    assert len(fs.pr_opened(case)) == 1
    assert not fs.full_baselines(case), "CI mode never captured a local full baseline for a partial selection"
    assert not fs.left_to_ci_records(case), (
        f"Only a whole-suite selection records the hand-off to PR CI: {fs.left_to_ci_records(case)}"
    )


def test_merge_owner_off_keeps_the_local_baseline_for_a_whole_suite_selection(proto, monkeypatch):
    case = fs.prepare(proto, monkeypatch)
    configure(case, merge_owner=False)

    outcome = fs.run(case)

    fs.assert_setup(case)
    assert outcome == "merged", f"Today's direct merge path: {outcome!r}\n{fs.log(case)[-2000:]}"
    assert fs.full_baselines(case), "With merge_owner off the local acceptance baseline is still captured"
    assert not fs.left_to_ci_records(case), fs.left_to_ci_records(case)
    assert (case.store.get_run(fs.N).comparison or {}).get("source") == "local"


def test_without_ci_keeps_the_local_baseline(proto, monkeypatch):
    case = fs.prepare(proto, monkeypatch)
    configure(case, checks="local")

    fs.run(case)  # This world's fake CI serves only CI mode; the capture happens first.

    assert fs.full_baselines(case), "Without CI the local acceptance baseline is still captured"
    assert not fs.left_to_ci_records(case), fs.left_to_ci_records(case)


def test_merge_owner_starts_no_local_full_baseline_for_the_queued_pr(proto, monkeypatch):
    case = fs.prepare(proto, monkeypatch)

    outcome = fs.run(case)
    fs.assert_setup(case)
    assert outcome == "merge-queued", fs.log(case)[-2000:]
    before = len(case.processes)
    for _ in range(4):
        support.gate(case)

    view = type("View", (), {"processes": case.processes[before:], "config": case.config})()
    assert not fs.full_baselines(view), f"The merge owner ran a local full baseline: {fs.full_baselines(view)}"
