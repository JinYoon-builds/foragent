"""Boundary cases for #1514 beyond its fixed acceptance scenarios."""
from datetime import timedelta
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'acceptance'))
from test_timeout_names import runner_case, run_and_observe, RUNTIME_MINUTES


def stand_and_follow(case):
    """The run's record after a PASS given with time left and a non-neutral base follow after it."""
    now = case.clock[0].isoformat()
    case.store.update_run(1, standing_pass_at=now, followed_base_at=now)


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
    if moved:  # the run's record: a PASS that stands, then a non-neutral base follow (#1514)
        stand_and_follow(case)
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
    if moved:  # the run's record: a PASS that stands, then a non-neutral base follow (#1514)
        stand_and_follow(case)
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
        case.store.update_run(1, standing_pass_at=case.clock[0].isoformat())

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
    # After a PASS with no implementer session the run is never too-large (#1514).
    expected = ('base-changed' if path.startswith('src/') else 'runtime-exceeded') if after_pass else 'too-large'
    assert limits == [300]
    assert outcome == expected
    assert case.tracker.get_issue(1).labels & {'blocked', 'running', 'ready'} == (
        {'ready'} if expected == 'base-changed' else {'blocked'}
    )
    flag = case.store.load_flag(1)
    assert (flag.reason if flag else None) == (None if expected == 'base-changed' else expected)


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
    if moved:  # the run's record: a PASS that stands, then a non-neutral base follow (#1514)
        stand_and_follow(case)
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
    if moved:  # the run's record: a PASS that stands, then a non-neutral base follow (#1514)
        stand_and_follow(case)
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
    if followed:
        stand_and_follow(case)
    else:
        case.store.update_run(1, standing_pass_at=case.clock[0].isoformat())
    case.clock[0] += timedelta(minutes=RUNTIME_MINUTES, seconds=1)
    with pytest.raises(ExecutorStopped) as caught:
        _implement(attempt, "Done when failed on the moved base")
    expected = "base-changed" if followed else "runtime-exceeded"
    assert caught.value.outcome == expected
    assert case.implementer.calls == []
    assert case.tracker.get_issue(1).labels & {"ready", "blocked"} == ({"ready"} if followed else {"blocked"})
    flag = case.store.load_flag(1)
    assert (flag.reason if flag else None) == (None if followed else "runtime-exceeded")


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
        seen.append(attempt.ctx.store.get_run(1).standing_pass_at is not None)
        raise ExecutorStopped("held", "stop after the first check")

    monkeypatch.setattr(runner, "_verify_implementation", first_check)
    monkeypatch.setattr(runner, "_implement", lambda *args, **kwargs: None)
    runner.run_issue(1, case.ctx)
    assert seen == [pushed is True]


RECORDS = {  # the run's own record before the continuation, and the name it must get (#1514)
    "no-pass": ((False, False), "too-large"),
    "pass": ((True, False), "runtime-exceeded"),
    "pass-then-follow": ((True, True), "base-changed"),
}


@pytest.mark.parametrize("record", sorted(RECORDS))
@pytest.mark.parametrize("kind", ["ci-repair", "base-follow", "review-rerun", "review-pass-then-ci-repair"])
def test_every_continuation_names_the_run_from_its_whole_record(git_history, tmp_path, monkeypatch, kind, record):
    """A continuation inside one run (CI repair, a neutral base refresh, a review rerun) that
    meets the end of the budget never resets what the run did before it: zero implementer
    sessions after the last PASS is never too-large (#1514)."""
    from foragent import runner
    from foragent.issue import parse_body
    from foragent.run_attempt import ExecutorStopped
    from foragent.run_checks import _verify_review_head
    from foragent.run_implementation import _implement, _prepare_implementation
    from foragent.run_publish_reviewed import _refresh_reviewed_head
    from foragent.run_review import _review_implementation

    (passed, followed), expected = RECORDS[record]
    case = runner_case(git_history, tmp_path, monkeypatch)
    case.git.fetch("origin", "main")
    worktree = case.store.worktree_path(1)
    case.git.add_worktree(worktree, "foragent/issue-1", "origin/main")
    case.store.start_run(1, branch="foragent/issue-1", worktree=worktree)
    git = case.git.at(worktree)
    now = case.clock[0].isoformat()
    case.store.update_run(1, standing_pass_at=now if passed else None, followed_base_at=now if followed else None)
    left = 0 if kind in {"ci-repair", "review-rerun"} else 300  # these steps start with a little time left
    case.clock[0] += timedelta(seconds=RUNTIME_MINUTES * 60 - left)

    if kind == "review-pass-then-ci-repair":
        # A re-review of the run's own head passes again, then a CI repair meets the end of the budget.
        attempt = _prepare_implementation(1, case.ctx, git, target_branch="main", capture_baseline=False)
        case.reviewer.steps = [lambda kwargs: "VERDICT: PASS"]
        _review_implementation(attempt)
        assert not attempt.failure
        if not passed:
            expected = "runtime-exceeded"  # this PASS is the run's first standing PASS
        case.clock[0] += timedelta(seconds=left)
        with pytest.raises(ExecutorStopped) as caught:
            runner._implement_and_publish(1, case.ctx, git, target_branch="main", failure="CI failed on the PR head")
        outcome = caught.value.outcome
    elif kind == "ci-repair":
        with pytest.raises(ExecutorStopped) as caught:
            runner._implement_and_publish(1, case.ctx, git, target_branch="main", failure="CI failed on the PR head")
        outcome = caught.value.outcome
    elif kind == "base-follow":
        head = git.head_sha()
        number = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Feature", body="Closes #1")
        case.tracker.set_pr_head_sha(number, head)
        git_history.update({"docs/guide.md": "Changed\n"}, message="Main advanced")  # neutral: no new follow
        case.git.fetch("origin", "main")

        def check_process(argv, *, timeout_s, **kwargs):
            case.clock[0] += timedelta(seconds=timeout_s)
            return -9, True, "Stopped at the run budget"

        monkeypatch.setattr(runner, "run_logged", check_process)
        parsed = parse_body(case.store.load_approval(1).body)
        _, outcome = _refresh_reviewed_head(
            1, case.ctx, git, case.tracker.get_pr(number), head, git_history.tip,
            lambda new: _verify_review_head(1, case.ctx, git, parsed, new, "PR link"))
    else:
        attempt = _prepare_implementation(1, case.ctx, git, target_branch="main", capture_baseline=False)
        case.reviewer.steps = [lambda kwargs: "VERDICT: FAIL\nsrc/feature.py:1 still wrong"]
        _review_implementation(attempt)
        with pytest.raises(ExecutorStopped) as caught:
            _implement(attempt, attempt.failure)
        outcome = caught.value.outcome
    assert outcome == expected
    assert case.implementer.calls == []
    flag = case.store.load_flag(1)
    assert (flag.reason if flag else None) == (None if expected == "base-changed" else expected)
    assert case.tracker.get_issue(1).labels & {"ready", "blocked"} == (
        {"ready"} if expected == "base-changed" else {"blocked"})


@pytest.mark.parametrize("path", ["docs/guide.md", "src/other_lane.py"])
def test_a_base_merge_that_spends_the_last_seconds_still_counts_as_following(git_history, tmp_path, monkeypatch, path):
    """The follow is recorded when it starts, so a merge that uses up the budget keeps it (#1514)."""
    from foragent import runner
    from foragent.git import GitRepo
    from foragent.issue import parse_body
    from foragent.run_checks import _verify_review_head
    from foragent.run_publish_reviewed import _refresh_reviewed_head

    case = runner_case(git_history, tmp_path, monkeypatch)
    head = case.git.head_sha()
    number = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Feature", body="Closes #1")
    case.tracker.set_pr_head_sha(number, head)
    git_history.update({path: "Changed\n"}, message="Main advanced")
    case.git.fetch("origin", "main")
    case.store.update_run(1, standing_pass_at=case.clock[0].isoformat())
    case.clock[0] += timedelta(seconds=RUNTIME_MINUTES * 60 - 30)
    original = GitRepo.merge_base

    def slow_merge(git, target):
        result = original(git, target)
        case.clock[0] += timedelta(seconds=60)  # the merge itself spends the last 30 seconds
        return result

    monkeypatch.setattr(GitRepo, "merge_base", slow_merge)
    monkeypatch.setattr(runner, "run_logged", lambda *a, **k: pytest.fail("no check starts without time"))
    parsed = parse_body(case.store.load_approval(1).body)
    _, outcome = _refresh_reviewed_head(
        1, case.ctx, case.git, case.tracker.get_pr(number), head, git_history.tip,
        lambda new: _verify_review_head(1, case.ctx, case.git, parsed, new, "PR link"))
    expected = "base-changed" if path.startswith("src/") else "runtime-exceeded"
    assert outcome == expected
    assert case.implementer.calls == []
    assert case.tracker.get_issue(1).labels & {"ready", "blocked"} == (
        {"ready"} if expected == "base-changed" else {"blocked"})
    flag = case.store.load_flag(1)
    assert (flag.reason if flag else None) == (None if expected == "base-changed" else expected)


def test_a_follow_before_any_pass_is_not_counted_after_a_later_pass(git_history, tmp_path, monkeypatch):
    """Only a follow that starts under a standing PASS can name the run base-changed (#1514)."""
    from foragent.run_waits import _timeout, record_base_follow, record_pass

    case = runner_case(git_history, tmp_path, monkeypatch)
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))
    record_base_follow(1, case.ctx)
    record_pass(1, case.ctx)
    assert case.store.get_run(1).followed_base_at is None
    assert _timeout(1, case.ctx) == "runtime-exceeded"
