"""Acceptance test for full-suite-baseline-to-ci (2026-11-08).

A lane run whose changed files select every acceptance file (a CI workflow change,
as #2768) opens its PR without capturing a local acceptance baseline and records
that the comparison is left to PR CI; PR CI's acceptance comparison then decides.

Real runner, merge owner, Git, state and pytest measurements (merge-owner prototype
world, CI mode); only GitHub, models and the clock are fake. Observed at public
boundaries: test processes the runner starts, the PR GitHub received, the run log,
the orchestrator journal and the run record.
"""

from acceptance.test_merge_path_real_settings import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401

import full_suite_ci_world as fs


def test_whole_suite_selection_opens_pr_without_local_baseline_and_leaves_comparison_to_pr_ci(
        proto, monkeypatch):
    case = fs.prepare(proto, monkeypatch)
    assert case.config.merge_owner and case.config.checks == "ci", "Setup: merge owner on, CI mode"

    outcome = fs.run(case)

    fs.assert_setup(case)
    assert len(fs.pr_opened(case)) == 1, (
        f"The run opens its PR exactly once; got {fs.pr_opened(case)}\n{fs.log(case)[-2000:]}"
    )
    assert not fs.full_baselines(case), (
        "A run whose selection is the whole acceptance suite captures no local acceptance baseline; "
        f"processes {fs.full_baselines(case)}\n{fs.log(case)[-2000:]}"
    )
    flag = case.store.load_flag(fs.N)
    assert outcome == "merge-queued" and flag is None, (
        f"The PR is handed to the merge queue for PR CI to decide; got {outcome!r}, flag {flag}\n"
        f"{fs.log(case)[-2000:]}"
    )
    records = fs.left_to_ci_records(case)
    assert records, (
        "The run records that the acceptance comparison is left to PR CI\n" + fs.log(case)[-2000:]
    )
    comparison = case.store.get_run(fs.N).comparison or {}
    assert comparison.get("source") != "local", (
        f"No local acceptance comparison was made, so none is recorded: {comparison}"
    )

