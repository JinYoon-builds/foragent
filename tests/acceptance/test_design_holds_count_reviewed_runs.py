"""Acceptance test for design-holds-count-reviewed-runs (architect r970/r996).

Since #2418 the runner holds a same-body retry before any work starts (the
repeat check) with the same reviewer hold reason as a hold after review. The
repeated_design_holds fact must not count such a pre-work repeat hold. Every
other reviewer-reason held run still counts, including holds built like the
locked test_review_holds_fact.py builds them (start_run, flag, finish_run).

Both kinds of hold come from the real runner with fake agents
(test_review_decision_hold_reader.pending_review_decision): a first run that the
reviewer holds after two failed rounds of one class, then retries that the
repeat check holds before an implementer starts. The test pins neither the
Reason value nor how the gate marks the pre-work hold. Observation goes through
the public `fa orch facts --json` and `fa orch roles --json` commands.
Break-it: counting every reviewer-reason hold (main), dropping the reviewer's own
hold, dropping a locked-style hold, or counting repeat holds must each fail.
"""

from datetime import timedelta

from foragent import resource_sample
from foragent.runner import run_issue
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_decision_hold_reader import pending_review_decision, queue_retry
from test_review_holds_fact import architect_met, design_holds, holds_case
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


def repeat_holds(runner_case, count):
    """Retries of the held issue that the repeat check holds before work starts."""
    implementer_calls = len(runner_case.implementer.calls)
    for _ in range(count):
        runner_case.clock[0] += timedelta(minutes=10)
        queue_retry(runner_case)
        assert run_issue(1, runner_case.ctx) == "held"
        runner_case.clock[0] += timedelta(minutes=1)
    assert len(runner_case.implementer.calls) == implementer_calls, (
        "Fixture: the repeat check must hold every retry before an implementer starts")


def observed_case(git_history, tmp_path, monkeypatch, retries):
    """The CLI case of the locked holds test, reading the real runner's state store."""
    root = tmp_path / "retries"
    root.mkdir()
    runner_case, _script, _request = pending_review_decision(git_history, root, monkeypatch)
    reviewer_run = runner_case.store.get_run(1)
    assert reviewer_run.outcome == "held", "Fixture: the reviewer must hold the first run"
    runner_case.reviewer_hold_reason = reviewer_run.hold_reason
    repeat_holds(runner_case, retries)
    case = holds_case(git_history, tmp_path, monkeypatch)
    case.runner = runner_case
    observe_runner_store(case, monkeypatch)
    return case


def observe_runner_store(case, monkeypatch):
    from foragent.state import StateStore

    runner = case.runner
    runner.clock[0] += timedelta(minutes=5)
    case.clock[0] = runner.clock[0]
    resource_sample.record(runner.store, ("normal", 0.1), runner.clock[0])
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kw: cls(runner.store.root, now=lambda: runner.clock[0])))


def locked_style_reviewer_hold(case, monkeypatch):
    """One more reviewer-reason hold, built as test_review_holds_fact.held builds them."""
    from foragent.actions import flag
    from foragent.reasons import Reason

    runner = case.runner
    runner.clock[0] += timedelta(minutes=10)
    queue_retry(runner)
    runner.store.start_run(1, branch="foragent/issue-1", worktree=runner.store.worktree_path(1))
    runner.clock[0] += timedelta(minutes=20)
    flag(runner.ctx, 1, Reason(runner.reviewer_hold_reason), "Reviewer still found defects after 3 rounds",
         "feature.txt:1 — the approved feature loses data\n\nhttps://github.com/owner/repo/pull/101")
    runner.store.finish_run(1, outcome="held")
    observe_runner_store(case, monkeypatch)


def test_one_reviewer_hold_and_a_later_pre_work_repeat_hold_are_not_reported(
        git_history, tmp_path, monkeypatch, capsys):
    case = observed_case(git_history, tmp_path, monkeypatch, retries=1)
    present, holds = design_holds(case, monkeypatch, capsys)
    assert not present and "#1" not in holds, (
        f"A retry the repeat check holds before work starts is not a design hold: {holds}")
    assert architect_met(case, monkeypatch, capsys) is False, (
        "One reviewer hold plus one pre-work repeat hold must not fire the architect alarm")


def test_several_pre_work_repeat_holds_add_nothing_to_the_count(git_history, tmp_path, monkeypatch, capsys):
    """The r938/r949/r969/r995 pattern: each retry of the held issue is held again before work."""
    case = observed_case(git_history, tmp_path, monkeypatch, retries=4)
    present, holds = design_holds(case, monkeypatch, capsys)
    assert not present and holds == {}, f"Pre-work repeat holds add no design holds: {holds}"
    assert architect_met(case, monkeypatch, capsys) is False
    # The reviewer's own hold still counts once, and so does a locked-style reviewer hold:
    # exactly two, whatever the number of repeat holds in between.
    locked_style_reviewer_hold(case, monkeypatch)
    present, holds = design_holds(case, monkeypatch, capsys)
    assert present and holds == {"#1": 2}, (
        f"Two reviewer holds count 2; the four pre-work repeat holds add nothing: {holds}")
    assert architect_met(case, monkeypatch, capsys) is True, (
        "A real repeat of reviewer holds still raises the architect alarm")
