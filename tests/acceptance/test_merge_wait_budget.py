"""Acceptance test for merge-wait-budget. Locked by Foragent; do not edit.

Run the real public issue runner, checks, review and publication with offline
Git transport, fake executors/tracker and a fake clock. Do not replace budget,
timeout, queue-wait or merge-policy code.

Break-it: returning "merged" without merging, merging before the predecessor
leaves, skipping the merge, or merging twice fails observable-effect checks.
The waits cover 65 minutes and the measured 826-minute incident maximum.
Removing the run budget globally fails the implementation-timeout control.
Crediting too much wait, resetting the budget, or freezing it after waiting
fails the post-wait active-work control. Poll frequency is not prescribed.
"""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import socket

import pytest

from conftest import git_command
from runner_support import run_case  # noqa: F401 -- shared offline runner fixture
from foragent.executors import ExecResult
from foragent.reasons import Reason
from foragent.runner import run_issue
from foragent import run_merge_turn


@pytest.fixture
def budget_case(run_case, monkeypatch):
    case = run_case
    case.ctx.config = replace(
        case.ctx.config, max_runtime_minutes=1, max_retries=0,
        executor="claude", reviewer="codex", skill_libraries=[],
        merge_group_max=1,
    )
    case.implementation_seconds = 40
    case.review_calls = []
    case.merge_calls = []
    case.wait_samples = []
    case.peer = None
    execute = case.executor.run

    def implement(**kwargs):
        case.now[0] += timedelta(seconds=case.implementation_seconds)
        return execute(**kwargs)

    def review(**kwargs):
        case.review_calls.append(kwargs)
        return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

    def no_network(*args, **kwargs):
        raise AssertionError("This acceptance test must stay offline")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(case.executor, "run", implement)
    reviewer = SimpleNamespace(run=review)
    case.ctx.executor_factory = lambda name: case.executor if name == "claude" else reviewer
    case.ctx.poll_hook = lambda execute, poll: execute()
    merge = case.tracker.merge_pr

    def merge_once(pr, *, expected_head_sha, **kwargs):
        if case.peer is not None:
            assert case.store.get_run(case.peer).status == "finished", (
                "A ready run must wait for its predecessor's merge turn"
            )
        assert case.tracker.pr_head_sha(pr) == expected_head_sha
        case.merge_calls.append((pr, expected_head_sha))
        return merge(pr, expected_head_sha=expected_head_sha, **kwargs)

    monkeypatch.setattr(case.tracker, "merge_pr", merge_once)
    return case


def queued_run(case, monkeypatch, wait_seconds, *, active_seconds_after_wait=0):
    """Release a live predecessor using public state APIs and fake time only."""
    case.peer = 2
    case.tracker.add_issue(2, "Earlier ready run", labels=["running"])
    case.store.start_run(2, branch="foragent/issue-2", worktree=case.store.worktree_path(2))
    run_merge_turn.join(
        case.ctx, run_merge_turn.seat(case.ctx, 2, "main"),
        case.ctx.git.ref_sha("origin/main"),
    )
    # FakeTracker allocates the next PR after the two existing issues.
    case.tracker.set_pr_files(3, ["feature.txt"])
    case.tracker.set_pr_checks(3, "pass")
    elapsed_wait = 0
    post_wait_work = []

    def release_peer():
        if case.store.get_run(2).status == "running":
            case.store.finish_run(2, outcome="merged")
            case.tracker.close_issue(2)
            case.tracker.remove_labels(2, ["running"])

    def sleep(seconds):
        nonlocal elapsed_wait
        assert seconds > 0, "Merge waiting must make progress on the fake clock"
        assert len(case.wait_samples) < wait_seconds + 100, "Merge wait did not finish after its turn became available"
        assert case.store.get_run(1).phase == "merge-lock-wait"
        assert len(case.executor.calls) == len(case.review_calls) == 1
        assert "Check exit_code=0" in case.store.log_path(1).read_text()
        pr = case.tracker.find_pr("foragent/issue-1")
        assert pr is not None, "Keep the completed implementation in a PR while waiting"
        assert case.tracker.merged_prs == []
        case.wait_samples.append(seconds)
        case.now[0] += timedelta(seconds=seconds)
        elapsed_wait += seconds
        if elapsed_wait >= wait_seconds:
            release_peer()

    checks = case.tracker.pr_checks

    def poll(pr):
        if (active_seconds_after_wait and not post_wait_work
                and case.store.get_run(2).status == "finished"):
            # Time actually spent checking the PR after the queue wait is work.
            case.now[0] += timedelta(seconds=active_seconds_after_wait)
            post_wait_work.append(active_seconds_after_wait)
        return checks(pr)

    case.ctx.sleep = sleep
    monkeypatch.setattr(case.tracker, "pr_checks", poll)
    try:
        outcome = run_issue(1, case.ctx)
    finally:
        release_peer()
    return outcome, elapsed_wait, post_wait_work


@pytest.mark.parametrize("wait_seconds", [3_900, 49_560], ids=["long-wait", "longer-wait"])
def test_completed_run_merges_once_after_wait_exceeds_implementation_budget(
    budget_case, monkeypatch, wait_seconds,
):
    case = budget_case
    outcome, waited, _ = queued_run(case, monkeypatch, wait_seconds)
    comments = "\n".join(comment.body for comment in case.tracker.list_comments(1))
    assert outcome == "merged", (
        f"A completed run must merge after waiting {wait_seconds}s for its turn; "
        f"got {outcome}: {comments}"
    )
    assert waited >= wait_seconds > case.ctx.config.max_runtime_minutes * 60
    assert len(case.executor.calls) == len(case.review_calls) == 1
    assert len(case.tracker.created_prs) == len(case.merge_calls) == 1
    pr, head = case.merge_calls[0]
    assert case.tracker.merged_prs == [pr]
    assert case.tracker.pr_head_sha(pr) == head
    assert git_command(case.history.origin, "show", f"{head}:feature.txt") == "done"
    assert case.store.get_run(1).outcome == "merged"
    assert case.tracker.get_issue(1).state == "closed"
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human", "running"}
    assert case.store.load_flag(1) is None
    assert "Too big for one session" not in comments
    assert "split it in order" not in comments


def assert_work_timeout(case, outcome):
    assert outcome == "timeout", "Actual work must still consume the run budget"
    assert case.store.get_run(1).outcome == "timeout"
    assert case.store.load_flag(1).reason == Reason.TIMEOUT
    assert "blocked" in case.tracker.get_issue(1).labels
    assert case.tracker.get_issue(1).state == "open"
    assert case.merge_calls == case.tracker.merged_prs == []
    comments = "\n".join(comment.body for comment in case.tracker.list_comments(1))
    assert "Too big for one session; split it in order" in comments


def test_implementation_over_budget_still_times_out(budget_case):
    case = budget_case
    case.implementation_seconds = 61
    outcome = run_issue(1, case.ctx)
    assert_work_timeout(case, outcome)
    assert len(case.executor.calls) == 1
    assert case.review_calls == case.wait_samples == []


def test_wait_preserves_only_the_unspent_budget_for_later_work(budget_case, monkeypatch):
    case = budget_case
    outcome, waited, post_wait_work = queued_run(
        case, monkeypatch, 90, active_seconds_after_wait=21,
    )
    assert waited >= 90 and post_wait_work == [21], (
        "The run must reach its turn before the later work consumes the last 20s"
    )
    assert_work_timeout(case, outcome)
    pr = case.tracker.find_pr("foragent/issue-1")
    assert pr is not None and len(case.tracker.created_prs) == 1
    assert len(case.executor.calls) == len(case.review_calls) == 1
