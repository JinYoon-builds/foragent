"""Boundary cases for #1514 beyond its fixed acceptance scenarios."""
from datetime import timedelta
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'acceptance'))
from test_timeout_names import runner_case, run_and_observe, RUNTIME_MINUTES


def test_base_follow_before_review_still_counts_as_own_work(git_history, tmp_path, monkeypatch):
    case = runner_case(git_history, tmp_path, monkeypatch)

    def implement_then_main_moves(kwargs):
        case.implement(kwargs)
        git_history.update({'src/other_lane.py': 'VALUE = 1\n'}, message='Main moved during implementation')
        case.clock[0] += timedelta(minutes=RUNTIME_MINUTES + 1)
        return 'Implementation used the session'

    case.implementer.steps = [implement_then_main_moves]
    observed = run_and_observe(case)
    assert observed['outcome'] == observed['stored_outcome'] == 'too-large'
    assert observed['reasons'] == ['too-large']
    assert observed['labels'] == ['blocked']


@pytest.mark.parametrize("moved", [False, True])
@pytest.mark.parametrize("budget", [300, 900, 1200])
def test_pr_head_deadline_names_the_limit_that_stopped_it(git_history, tmp_path, monkeypatch, moved, budget):
    from foragent import runner
    from foragent.issue import parse_body
    from foragent.run_checks import _verify_review_head

    case = runner_case(git_history, tmp_path, monkeypatch)
    case.clock[0] += timedelta(seconds=RUNTIME_MINUTES * 60 - budget)
    case.ctx._budget_base_changed = 1 if moved else None
    limits = []

    def check_process(argv, *, timeout_s, **kwargs):
        limits.append(timeout_s)
        case.clock[0] += timedelta(seconds=timeout_s)
        return -9, True, "Stopped at the supplied deadline"

    monkeypatch.setattr(runner, "run_logged", check_process)
    parsed = parse_body(case.store.load_approval(1).body)
    outcome = _verify_review_head(1, case.ctx, case.git, parsed, case.git.head_sha(), "PR link")
    expected = "timeout" if budget >= 900 else ("base-changed" if moved else "too-large")
    assert limits == [min(900, budget)]
    assert outcome == expected
    flag = case.store.load_flag(1)
    assert (flag.reason if flag else None) == (None if expected == "base-changed" else expected)
    assert case.tracker.get_issue(1).labels & {"ready", "running", "blocked"} == (
        {"ready"} if expected == "base-changed" else {"blocked"}
    )


@pytest.mark.parametrize("moved", [False, True])
@pytest.mark.parametrize("budget,limits", [(300, [300]), (900, [900]), (2400, [900, 1500]), (3600, [900, 1800])])
def test_protected_deadline_names_initial_or_retry_cap(git_history, tmp_path, monkeypatch, moved, budget, limits):
    from types import SimpleNamespace
    from foragent import runner
    from foragent.run_attempt import ExecutorStopped
    from foragent.run_baseline import AcceptanceBaseline
    from foragent.run_checks import _check_protected

    case = runner_case(git_history, tmp_path, monkeypatch)
    case.clock[0] += timedelta(seconds=RUNTIME_MINUTES * 60 - budget)
    case.ctx._budget_base_changed = 1 if moved else None
    baseline = AcceptanceBaseline("pytest", ("test_keep.py::test_a",), duration_seconds=450)
    attempt = SimpleNamespace(n=1, ctx=case.ctx, baseline=baseline, worktree=case.root,
                              log=case.store.log_path(1), checks=[])
    calls = []

    def check_process(argv, *, timeout_s, **kwargs):
        calls.append(timeout_s)
        case.clock[0] += timedelta(seconds=timeout_s)
        return -9, True, "Stopped at the supplied deadline"

    monkeypatch.setattr(runner, "run_logged", check_process)
    with pytest.raises(ExecutorStopped) as caught:
        _check_protected(attempt)
    expected = ("base-changed" if moved else "too-large") if budget in {300, 2400} else "timeout"
    assert calls == limits
    assert caught.value.outcome == expected
    flag = case.store.load_flag(1)
    assert (flag.reason if flag else None) == (None if expected == "base-changed" else expected)
    assert case.tracker.get_issue(1).labels & {"ready", "running", "blocked"} == (
        {"ready"} if expected == "base-changed" else {"blocked"}
    )
    assert case.store.get_run(1).resume_whole
    assert "test_keep.py::test_a" in case.store.get_run(1).resume_reason


@pytest.mark.parametrize('after_pass', [False, True])
@pytest.mark.parametrize('path', ['docs/guide.md', 'src/other_lane.py'])
def test_reviewed_refresh_counts_only_non_neutral_moves_after_pass(
    git_history, tmp_path, monkeypatch, after_pass, path,
):
    from foragent import runner
    from foragent.issue import parse_body
    from foragent.run_checks import _verify_review_head
    from foragent.run_publish_reviewed import _refresh_reviewed_head

    case = runner_case(git_history, tmp_path, monkeypatch)
    head = case.git.head_sha()
    number = case.tracker.create_pr(head='foragent/issue-1', base='main', title='Feature', body='Closes #1')
    pr = case.tracker.get_pr(number)
    case.tracker.set_pr_head_sha(number, head)
    git_history.update({path: 'Changed\n'}, message='Main advanced')
    case.git.fetch('origin', 'main')
    if after_pass:
        case.ctx._budget_review_pass = 1

    case.clock[0] += timedelta(seconds=RUNTIME_MINUTES * 60 - 300)
    limits = []

    def check_process(argv, *, timeout_s, **kwargs):
        limits.append(timeout_s)
        case.clock[0] += timedelta(seconds=timeout_s)
        return -9, True, "Stopped at the run budget"

    monkeypatch.setattr(runner, "run_logged", check_process)
    parsed = parse_body(case.store.load_approval(1).body)

    def reverify(new_head):
        return _verify_review_head(1, case.ctx, case.git, parsed, new_head, "PR link")

    _, outcome = _refresh_reviewed_head(1, case.ctx, case.git, pr, head, git_history.tip, reverify)
    expected = 'base-changed' if after_pass and path.startswith('src/') else 'too-large'
    assert limits == [300]
    assert outcome == expected
    assert case.tracker.get_issue(1).labels & {'blocked', 'running', 'ready'} == (
        {'ready'} if expected == 'base-changed' else {'blocked'}
    )
    flag = case.store.load_flag(1)
    assert (flag.reason if flag else None) == (None if expected == 'base-changed' else 'too-large')


def test_review_that_used_whole_budget_is_too_large_even_if_main_moves(git_history, tmp_path, monkeypatch):
    case = runner_case(git_history, tmp_path, monkeypatch)

    def review_all_day(kwargs):
        git_history.update({"src/other_lane.py": "VALUE = 1\n"}, message="Main advanced")
        case.clock[0] += timedelta(minutes=RUNTIME_MINUTES + 1)
        return "VERDICT: PASS"

    case.reviewer.steps = [review_all_day]
    observed = run_and_observe(case)
    assert observed["outcome"] == observed["stored_outcome"] == "too-large"
    assert observed["reasons"] == ["too-large"]
    assert observed["labels"] == ["blocked"]


@pytest.mark.parametrize("moved", [False, True])
def test_optional_local_check_uses_run_budget_not_done_when_deadline(git_history, tmp_path, monkeypatch, moved):
    from dataclasses import replace
    from foragent import ci_local, runner
    from foragent.run_attempt import ExecutorStopped
    from foragent.run_checks import _verify_contents
    from foragent.run_implementation import _prepare_implementation

    case = runner_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.ctx.config, test_command="pytest")
    attempt = _prepare_implementation(1, case.ctx, case.git, target_branch="main", capture_baseline=False)
    (case.root / "feature.txt").write_text("done\n")
    case.clock[0] += timedelta(seconds=RUNTIME_MINUTES * 60 - 300)
    case.ctx._budget_base_changed = 1 if moved else None
    limits = []

    def check_process(argv, *, timeout_s, **kwargs):
        if "-m local_only" not in argv[-1] or "--collect-only" in argv[-1]:
            return 0, False, ""
        limits.append(timeout_s)
        case.clock[0] += timedelta(seconds=timeout_s)
        return -9, True, "Optional check hit the run budget"

    monkeypatch.setattr(ci_local, "_uses_local_only", lambda root: True)
    monkeypatch.setattr(runner, "run_logged", check_process)
    with pytest.raises(ExecutorStopped) as caught:
        _verify_contents(attempt)
    expected = "base-changed" if moved else "too-large"
    assert limits == [300]
    assert caught.value.outcome == expected
    flag = case.store.load_flag(1)
    assert (flag.reason if flag else None) == (None if moved else "too-large")
    assert case.tracker.get_issue(1).labels & {"ready", "running", "blocked"} == (
        {"ready"} if moved else {"blocked"}
    )


@pytest.mark.parametrize("moved", [False, True])
@pytest.mark.parametrize("step", ["capture", "collection"])
@pytest.mark.parametrize("cut_by", ["run-budget", "already-spent", "own-limit"])
def test_baseline_measurement_cut_by_the_run_budget_names_the_run(
    git_history, tmp_path, monkeypatch, step, moved, cut_by,
):
    """A base measurement limited by the run's remaining time and cut there spent the run
    budget: too-large (base-changed after PASS and base following), not an unavailable baseline.
    A cut at the measurement's own limit, with run time left, stays an unavailable baseline."""
    from foragent import runner
    from foragent.run_attempt import AttemptFailure, ExecutorStopped
    from foragent.run_baseline import AcceptanceBaseline, _capture_acceptance_baseline, collect_protected_ids
    from foragent.run_baseline_limits import capture_limit

    case = runner_case(git_history, tmp_path, monkeypatch)
    own = capture_limit(None)
    budget = {"run-budget": 300, "already-spent": 0}.get(cut_by, own + 600)
    case.clock[0] += timedelta(seconds=RUNTIME_MINUTES * 60 - budget)
    case.ctx._budget_base_changed = 1 if moved else None
    limits = []

    def check_process(argv, *, timeout_s, **kwargs):
        limits.append(timeout_s)
        case.clock[0] += timedelta(seconds=timeout_s)
        return -9, True, "Stopped at the supplied deadline"

    # Another lane's acceptance test, so the base has a protected set to measure.
    git_history.update({"tests/acceptance/test_other.py": "def test_other():\n    pass\n"}, message="Other lane")
    case.git.fetch("origin", "main")
    monkeypatch.setattr(runner, "run_logged", check_process)
    sha = git_history.tip
    command = f"{sys.executable} -m pytest"
    previous = AcceptanceBaseline(command, ("tests/acceptance/test_feature.py::test_feature",), base_sha=sha)
    measure = ((lambda: _capture_acceptance_baseline(1, case.ctx, command, sha)) if step == "capture"
               else (lambda: collect_protected_ids(1, case.ctx, previous, sha)))
    if cut_by == "own-limit":
        with pytest.raises(AttemptFailure):
            measure()
        assert limits == [own]
        assert case.store.load_flag(1) is None
        return
    with pytest.raises(ExecutorStopped) as caught:
        measure()
    expected = "base-changed" if moved else "too-large"
    assert limits == ([300] if budget else [])
    assert caught.value.outcome == expected
    flag = case.store.load_flag(1)
    assert (flag.reason if flag else None) == (None if moved else "too-large")
    assert case.tracker.get_issue(1).labels & {"ready", "running", "blocked"} == (
        {"ready"} if moved else {"blocked"})


@pytest.mark.parametrize("followed", [False, True])
def test_no_session_left_after_a_pass_keeps_the_runs_attribution(git_history, tmp_path, monkeypatch, followed):
    """After a PASS the run followed a moved base and its rechecks failed, so the loop came back
    to implementation with no run time left. No implementer session starts, so the budget went
    to what the run did last: following the base is base-changed, and without it too-large (#1514)."""
    from foragent.run_attempt import ExecutorStopped
    from foragent.run_implementation import _implement, _prepare_implementation

    case = runner_case(git_history, tmp_path, monkeypatch)
    case.git.fetch("origin", "main")
    worktree = case.store.worktree_path(1)
    case.git.add_worktree(worktree, "foragent/issue-1", "origin/main")
    case.store.start_run(1, branch="foragent/issue-1", worktree=worktree)
    attempt = _prepare_implementation(1, case.ctx, case.git.at(worktree), target_branch="main",
                                      capture_baseline=False)
    case.ctx._budget_review_pass = 1
    case.ctx._budget_base_changed = 1 if followed else None
    case.clock[0] += timedelta(minutes=RUNTIME_MINUTES, seconds=1)
    with pytest.raises(ExecutorStopped) as caught:
        _implement(attempt, "Done when failed on the moved base")
    expected = "base-changed" if followed else "too-large"
    assert caught.value.outcome == expected
    assert case.implementer.calls == []
    assert case.tracker.get_issue(1).labels & {"ready", "blocked"} == ({"ready"} if followed else {"blocked"})


@pytest.mark.parametrize("pushed", [True, None])
def test_a_resumed_reviewed_push_keeps_its_pass_for_later_base_following(git_history, tmp_path, monkeypatch, pushed):
    """A reviewed head whose publication push resumed after an outage reuses that PASS; budget
    then spent following a moved base must still count as after that PASS (#1514)."""
    from foragent import orch_github, runner
    from foragent.run_attempt import ExecutorStopped

    case = runner_case(git_history, tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(orch_github, "resume_push", lambda ctx, n, git: pushed)

    def first_check(attempt):
        seen.append(getattr(attempt.ctx, "_budget_review_pass", None))
        raise ExecutorStopped("held", "stop after the first check")

    monkeypatch.setattr(runner, "_verify_implementation", first_check)
    monkeypatch.setattr(runner, "_implement", lambda *args, **kwargs: None)
    runner.run_issue(1, case.ctx)
    assert seen == [1 if pushed else None]
