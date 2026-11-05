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


def test_a_stop_during_the_implementer_turn_skips_its_checks(case, monkeypatch):
    from foragent import run_checks

    monkeypatch.setattr(run_checks, "run_done_when", lambda *a, **k: pytest.fail("No checks after a stop"))

    def implement(kwargs):
        case.implement(kwargs)
        _stop(case)
        return "Checkpoint saved"

    case.implementer.steps = [implement]
    _assert_stopped_with_work(case, run_issue(1, case.ctx))


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
