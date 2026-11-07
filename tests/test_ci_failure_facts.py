"""CI-reported code failures of a run's head are kept as one local ``ci-failure`` fact.

The scorecard counts a later round of the same run as rework only after such a fact
(#1727). The shared offline CI scenario uses the real CI comparison.
"""

from helpers.ci_comparison import ci_evidence_recording, mode_case, no_external_execution, run
from helpers.home import isolated_home, offline_acceptance


def facts(case):
    return [(f["issue"], f["run_started_at"], f["data"]) for f in case.store.flow_records()
            if f["kind"] == "ci-failure"]


def regression_case(git_history, tmp_path, monkeypatch):
    """CI's acceptance comparison reports a lost passing test for every head; the repair does not fix it."""
    case = mode_case(git_history, tmp_path, monkeypatch)
    case.ci.regression = "nonpassing"
    case.implementer.steps.append(lambda kwargs: "No real fix")
    case.reviewer.steps += ["VERDICT: PASS"] * 4
    return case


def test_ci_comparison_regression_in_the_flow_path_is_kept_once_per_head(git_history, tmp_path, monkeypatch):
    case = regression_case(git_history, tmp_path, monkeypatch)
    assert run(case) == "retries-exceeded"
    assert len(case.implementer.calls) == 2, "The lost test sends the run back for a same-run repair"
    started = case.store.get_run(1).started_at
    heads = list(dict.fromkeys(head for head, _ in case.ci.reads))
    assert len(heads) == 2
    assert facts(case) == [(1, started, {"pr": 2, "head": head}) for head in heads]


def test_ci_comparison_regression_in_the_review_path_is_kept(git_history, tmp_path, monkeypatch):
    from datetime import timedelta
    from foragent.runner import review_only

    case = regression_case(git_history, tmp_path, monkeypatch)
    assert run(case) == "retries-exceeded"
    before = facts(case)
    head = case.tracker.pr_head_sha(2)
    monkeypatch.setattr(case.tracker, "current_issue", case.tracker.get_issue, raising=False)
    case.clock[0] += timedelta(minutes=5)
    assert review_only(1, case.ctx, repair_ci=False) == "ci-failed"
    started = case.store.get_run(1).started_at
    assert started != before[0][1], "The review run is a run of its own"
    assert facts(case) == [*before, (1, started, {"pr": 2, "head": head})]
