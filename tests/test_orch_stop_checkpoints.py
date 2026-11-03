"""A project stop requested at any step boundary never publishes or merges (#1491).

The run's owner, its runner, reads the request before every further executor turn
and before every publication effect, then saves and finishes the run itself.
All Git and executor dependencies use the offline acceptance adapters.
"""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from conftest import git_command  # noqa: E402
from foragent import orch, run_publish  # noqa: E402
from foragent.runner import run_issue  # noqa: E402
from test_orch_review_handoff import make_case  # noqa: E402

WHY = "Jin requested a stop"


def _stop(case):
    orch.stop_all(case.ctx, WHY)


def _assert_stopped_with_work(case, outcome):
    assert outcome == "stopped"
    run = case.store.get_run(1)
    assert (run.status, run.outcome, run.detail) == ("finished", "stopped", WHY)
    saved = git_command(case.root, "show", "refs/heads/foragent/issue-1:feature.txt")
    assert saved == "done", "The owner keeps the unfinished work on its branch"
    remote = git_command(case.history.origin, "show", "refs/heads/foragent/issue-1:feature.txt")
    assert remote == "done", "The WIP save still pushes the branch after the stop"


@pytest.fixture
def case(git_history, tmp_path, monkeypatch):
    return make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")


def test_a_stop_during_review_never_publishes(case):
    def review(kwargs):
        _stop(case)
        return "VERDICT: PASS"

    case.reviewer.steps = [review]
    outcome = run_issue(1, case.ctx)
    _assert_stopped_with_work(case, outcome)
    assert case.tracker.find_pr("foragent/issue-1") is None


def test_a_stop_during_verification_skips_the_review_turn(case, monkeypatch):
    from foragent import run_checks

    real = run_checks.run_done_when

    def checks(*args, **kwargs):
        _stop(case)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_checks, "run_done_when", checks)
    case.reviewer.steps = []  # Any review call fails the test.
    outcome = run_issue(1, case.ctx)
    _assert_stopped_with_work(case, outcome)
    assert case.reviewer.calls == []
    assert case.tracker.find_pr("foragent/issue-1") is None


def test_a_stop_while_publication_waits_opens_no_pr(case, monkeypatch):
    real = run_publish.wait_for_overlap

    def wait(attempt):
        _stop(case)
        return real(attempt)

    monkeypatch.setattr(run_publish, "wait_for_overlap", wait)
    outcome = run_issue(1, case.ctx)
    _assert_stopped_with_work(case, outcome)
    assert case.tracker.find_pr("foragent/issue-1") is None


def test_a_stop_just_before_the_merge_leaves_the_pr_unmerged(case, monkeypatch):
    real = run_publish.retitle_for_merge
    merged = []
    monkeypatch.setattr(case.tracker, "merge_pr", lambda *a, **k: merged.append(a))

    def retitle(*args, **kwargs):
        _stop(case)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_publish, "retitle_for_merge", retitle)
    outcome = run_issue(1, case.ctx)
    assert merged == [], "A project stop never merges"
    assert outcome == "stopped"
    assert case.tracker.find_pr("foragent/issue-1") is not None


def test_a_stop_during_the_implementer_turn_skips_its_checks(case, monkeypatch):
    from foragent import run_checks

    monkeypatch.setattr(run_checks, "run_done_when", lambda *a, **k: pytest.fail("No checks after a stop"))

    def implement(kwargs):
        case.implement(kwargs)
        _stop(case)
        return "Checkpoint saved"

    case.implementer.steps = [implement]
    _assert_stopped_with_work(case, run_issue(1, case.ctx))


def test_a_held_head_is_not_published_after_a_stop(case):
    from types import SimpleNamespace

    from foragent import harness_guard
    from foragent.run_attempt import ExecutorStopped

    _stop(case)
    calls = []
    ctx = SimpleNamespace(store=case.store, tracker=SimpleNamespace(
        get_issue=lambda n: calls.append("get_issue"), create_pr=lambda **k: calls.append("create_pr")))
    with pytest.raises(ExecutorStopped) as stopped:
        harness_guard.publish_hold(SimpleNamespace(n=1, ctx=ctx))
    assert (stopped.value.outcome, str(stopped.value)) == ("stopped", WHY)
    assert calls == []


def test_a_stop_before_a_reviewed_merge_leaves_the_pr_unmerged(case, monkeypatch):
    from foragent.runner import review_only

    real = run_publish.retitle_for_merge
    monkeypatch.setattr(run_publish, "retitle_for_merge", lambda *a, **k: (_stop(case), real(*a, **k))[1])
    assert run_issue(1, case.ctx) == "stopped"  # The first run opens the PR and stops before merging.
    pr = case.tracker.find_pr("foragent/issue-1")
    head = case.tracker.pr_head_sha(pr)
    case.tracker.remove_labels(1, ["running"])
    from datetime import timedelta

    case.clock[0] += timedelta(minutes=5)
    orch.resume_all(case.ctx)  # The review run starts after the first stop was lifted.
    merged = []
    monkeypatch.setattr(case.tracker, "merge_pr", lambda *a, **k: merged.append(a))
    outcome = review_only(1, case.ctx, verdict_head=head)
    assert merged == [], "A stop requested during the reviewed path never merges"
    assert outcome == "stopped"


def test_a_stop_during_a_failing_review_starts_no_new_implementer_turn(case):
    def review(kwargs):
        _stop(case)
        return "VERDICT: FAIL\nfeature.txt:1 — handle the empty case"

    case.reviewer.steps = [review]
    case.implementer.steps = [case.implement, case.implement]
    _assert_stopped_with_work(case, run_issue(1, case.ctx))
    assert len(case.implementer.calls) == 1, "No implementer turn starts after the stop"


def test_a_stop_during_failing_checks_starts_no_new_implementer_turn(case, monkeypatch):
    from foragent import run_checks

    real = run_checks.run_done_when

    def checks(*args, **kwargs):
        _stop(case)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_checks, "run_done_when", checks)

    def unfinished(kwargs):
        (kwargs["worktree"] / "notes.txt").write_text("half done\n")  # feature.txt is still missing.
        return "Implemented"

    case.implementer.steps = [unfinished, case.implement]
    case.reviewer.steps = []
    assert run_issue(1, case.ctx) == "stopped"
    assert len(case.implementer.calls) == 1, "No implementer turn starts after the stop"
    assert git_command(case.root, "show", "refs/heads/foragent/issue-1:notes.txt") == "half done"


def test_every_executor_launch_checks_the_request_first(case):
    from foragent.run_attempt import ExecutorStopped
    from foragent.run_execution import _available_executor

    _stop(case)
    with pytest.raises(ExecutorStopped) as stopped:
        _available_executor(1, case.ctx, case.implementer, worktree=case.root,
                            prompt="resolve the conflict", log_path=case.store.log_path(1))
    assert stopped.value.outcome == "stopped"
    assert case.implementer.calls == [], "Conflict, rework and base-move turns all start here"


def test_a_stop_during_review_never_pushes_a_new_head_to_an_open_pr(case, monkeypatch):
    from datetime import timedelta

    real = run_publish.retitle_for_merge
    with monkeypatch.context() as first:
        first.setattr(run_publish, "retitle_for_merge", lambda *a, **k: (_stop(case), real(*a, **k))[1])
        assert run_issue(1, case.ctx) == "stopped"  # Opens the PR, then stops before merging.
    pr = case.tracker.find_pr("foragent/issue-1")
    published = "0" * 40  # The PR shows an older head, so the next run implements and reviews again.
    case.tracker.set_pr_head_sha(pr, published)
    case.tracker.remove_labels(1, ["running"])
    case.clock[0] += timedelta(minutes=5)
    orch.resume_all(case.ctx)

    def rework(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("done twice\n")
        return "Implemented again"

    def review(kwargs):
        _stop(case)
        return "VERDICT: PASS"

    case.implementer.steps, case.reviewer.steps = [rework], [review]
    monkeypatch.setattr(run_publish, "retitle_for_merge", real)
    assert run_issue(1, case.ctx) == "stopped"
    assert case.reviewer.steps == [], "The review that requested the stop ran"
    assert case.tracker.pr_head_sha(pr) == published, "The open PR keeps the head it had"


def test_the_wip_save_still_pushes_after_a_stop(case):
    from types import SimpleNamespace

    from foragent.run_attempt import ExecutorStopped

    _stop(case)
    pushes = []
    git = SimpleNamespace(push=lambda branch, **k: pushes.append(branch), head_sha=lambda: "abc")
    attempt = SimpleNamespace(n=1, ctx=case.ctx, git=git, branch="foragent/issue-1", rebuilt=False)
    with pytest.raises(ExecutorStopped):
        run_publish._push_branch(attempt)
    assert pushes == []
    run_publish._push_branch(attempt, saving=True)
    assert pushes == ["foragent/issue-1"]


def test_a_reviewed_base_refresh_does_not_push_after_a_stop(case, monkeypatch):
    from types import SimpleNamespace

    from foragent import runner
    from foragent.run_attempt import ExecutorStopped
    from foragent.run_publish_reviewed import _refresh_reviewed_head

    case.store.start_run(1, branch="foragent/issue-1", worktree=case.root)
    pushes = []
    git = SimpleNamespace(head_sha=lambda: "head", merge_base=lambda sha: None,
                          push_head=lambda *a, **k: pushes.append(a))
    monkeypatch.setattr(runner, "_remaining", lambda n, ctx: 600)
    monkeypatch.setattr(run_publish, "_check_publication", lambda *a: None)
    monkeypatch.setattr(run_publish, "_phase", lambda *a: None)
    monkeypatch.setattr(case.tracker, "pr_head_sha", lambda pr: "head")
    pr = SimpleNamespace(number=9, base="main", head="foragent/issue-1")

    def reverify(head):
        _stop(case)
        return None

    with pytest.raises(ExecutorStopped):
        _refresh_reviewed_head(1, case.ctx, git, pr, "head", "base", reverify)
    assert pushes == []
