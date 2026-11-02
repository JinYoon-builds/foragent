"""Draft locks: GitHub outages pause work and preserve completed review.

Use public runner/tick entry points. Only transport, time, Git publication and
executors are offline fakes; assertions concern durable results and effects.
"""

from dataclasses import replace
from datetime import datetime, timedelta
import subprocess

import pytest

from foragent.runner import review_only, run_issue
from foragent.scheduler import tick
from foragent.state import StateStore
from foragent.tracker import GhTracker
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home  # noqa: F401


CONNECTION = 'error connecting to api.github.com: dial tcp: connection refused'


def outage_case(git_history, tmp_path, monkeypatch, entry):
    case = make_case(
        git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
        lanes={"orch": {"paths": ["**"]}}, merge_group_max=1,
    )
    case.tracker.add_labels(1, ["lane:orch"])
    case.tracker.record_label(1, "approved", "human")
    if entry != "run":
        case.ctx.config = replace(case.config, review="careful", reviewer="none")
        assert run_issue(1, case.ctx) == "pr-open"
        case.ctx.config = case.config
    case.store.update_run(1, lane="orch", attempts=3, restarts=1)
    case.implementer.steps = [case.implement, case.implement]
    case.errors, case.transport_calls = [], []
    case.issue = case.tracker.get_issue
    return case


def interrupt(case, monkeypatch, operation, message, recovered):
    original = getattr(case.tracker, operation)

    def transport(argv, input=None):
        case.transport_calls.append(list(argv))
        case.errors.append(message)
        raise subprocess.CalledProcessError(1, ["gh", *argv], stderr=message)

    github = GhTracker("owner/repo", run=transport)

    def call(*args, **kwargs):
        if case.clock[0] < recovered:
            # Exercise the real gh-to-TrackerError conversion, not a fabricated
            # future exception API. This read never reaches the network.
            return getattr(github, operation)(*args, **kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(case.tracker, operation, call)
    # A healthy rate-limit endpoint cannot explain a connection/server outage.
    monkeypatch.setattr(case.tracker, "rate_limit_reset", lambda: None)


def assert_wait(case, message):
    persisted = StateStore(case.store.root, now=lambda: case.clock[0])
    run = persisted.get_run(1)
    assert run.status == "finished" and run.outcome == "limit-wait"
    assert run.waiting_until and datetime.fromisoformat(run.waiting_until) > case.clock[0]
    assert persisted.load_flag(1) is None
    assert not case.issue(1).labels & {"blocked", "needs-human"}
    assert message in run.detail
    log = persisted.log_path(1).read_text()
    assert message in log
    assert "GitHub rate limit:" not in log, "An outage must retain its actual diagnosis"
    case.store = case.ctx.store = persisted
    return datetime.fromisoformat(run.waiting_until)


def restart_at_wait_end(case, before_run=lambda: None):
    launches = []
    context = tick_context(case)
    context.launcher = lambda n: launches.append(n) or 7000 + n
    tick(context)
    assert launches == [1], "The ordinary tick must restart the waiting issue without a person"
    before_run()
    return run_issue(1, case.ctx)


def assert_merged_once(case):
    assert case.store.get_run(1).outcome == "merged"
    assert case.tracker.get_issue(1).state == "closed"
    assert not case.tracker.get_issue(1).labels & {"running", "blocked", "needs-human"}
    assert case.store.load_flag(1) is None
    assert len(case.tracker.merged_prs) == 1


@pytest.mark.parametrize("entry", ["run", "review", "review-admission"])
def test_four_minute_connection_drop_resumes_without_a_person(
    git_history, tmp_path, monkeypatch, entry,
):
    case = outage_case(git_history, tmp_path, monkeypatch, entry)
    recovered = case.clock[0] + timedelta(minutes=4)
    # Review has already claimed its run before reading the PR list.
    interrupt(case, monkeypatch, "list_prs" if entry == "review" else "get_issue", CONNECTION, recovered)
    initial = run_issue if entry == "run" else review_only
    assert initial(1, case.ctx) == "limit-wait"
    assert case.errors
    until = assert_wait(case, CONNECTION)
    before = case.store.get_run(1)
    case.clock[0] = until - timedelta(microseconds=1)
    calls = list(case.transport_calls)
    launches = []
    context = tick_context(case)
    context.launcher = launches.append
    tick(context)
    assert launches == [] and case.transport_calls == calls
    case.clock[0] = max(until, recovered)
    assert restart_at_wait_end(case) == "merged"
    assert_merged_once(case)
    assert case.store.get_run(1).attempts == before.attempts
    assert case.store.get_run(1).restarts == before.restarts
    assert len(case.reviewer.calls) == 1


@pytest.mark.parametrize("entry", ["run", "review"])
@pytest.mark.parametrize("diagnostic", [
    CONNECTION,
    "gh: HTTP 500 Internal Server Error",
    "gh: HTTP 502 Bad Gateway",
    "gh: HTTP 503 Service Unavailable",
    "gh: HTTP 504 Gateway Timeout",
])
def test_passed_head_returns_to_ci_without_another_implementation_or_review(
    git_history, tmp_path, monkeypatch, entry, diagnostic,
):
    case = outage_case(git_history, tmp_path, monkeypatch, entry)
    recovered = case.clock[0] + timedelta(minutes=4)
    interrupt(case, monkeypatch, "pr_checks", diagnostic, recovered)
    initial = run_issue if entry == "run" else review_only
    assert initial(1, case.ctx) == "limit-wait"
    assert case.errors and len(case.reviewer.calls) == 1
    until = assert_wait(case, diagnostic)
    before = case.store.get_run(1)
    pr = case.tracker.created_prs[0].number
    head = case.tracker.pr_head_sha(pr)
    implementations, reviews = len(case.implementer.calls), len(case.reviewer.calls)
    if diagnostic == CONNECTION:
        # Retry while the link is still down too. Every retry must return to
        # the same durable wait without repeating work that already passed.
        for _ in range(8):
            if until >= recovered:
                break
            case.clock[0] = until
            assert restart_at_wait_end(case) == "limit-wait"
            until = assert_wait(case, diagnostic)
            assert len(case.implementer.calls) == implementations
            assert len(case.reviewer.calls) == reviews
    case.clock[0] = max(until, recovered)
    assert restart_at_wait_end(case) == "merged"
    assert_merged_once(case)
    assert case.tracker.pr_head_sha(pr) == head, "Resume the exact head that passed review"
    assert len(case.implementer.calls) == implementations
    assert len(case.reviewer.calls) == reviews
    assert (case.store.get_run(1).attempts, case.store.get_run(1).restarts) == (
        before.attempts, before.restarts,
    )


@pytest.mark.parametrize("change", ["head", "approved-body", "head-during-resume"])
def test_pass_before_a_drop_cannot_waive_review_of_changed_work(
    git_history, tmp_path, monkeypatch, change,
):
    from conftest import git_command
    from foragent.issue import body_hash

    case = outage_case(git_history, tmp_path, monkeypatch, "run")
    recovered = case.clock[0] + timedelta(minutes=4)
    interrupt(case, monkeypatch, "pr_checks", CONNECTION, recovered)
    assert run_issue(1, case.ctx) == "limit-wait"
    until = assert_wait(case, CONNECTION)
    assert len(case.reviewer.calls) == 1
    before_run = lambda: None
    if change in {"head", "head-during-resume"}:
        # Advance the saved issue branch using fixture object import only.
        old_main = case.history.tip
        case.history.tip = case.git.local_branch_sha("foragent/issue-1")
        case.history.update({"extra.txt": "changed since review\n"})
        changed = case.history.tip
        git_command(case.history.origin, "update-ref", "refs/heads/foragent/issue-1", changed)
        git_command(case.history.origin, "update-ref", "refs/heads/main", old_main)
        case.history.tip = old_main
        case.git.fetch("origin", "foragent/issue-1")
        if change == "head":
            git_command(case.root, "update-ref", "refs/heads/foragent/issue-1", changed)
            case.tracker.set_pr_head_sha(case.tracker.created_prs[0].number, changed)
        else:
            # Change the remote immediately after its first recovery read.
            # Both reads go through the real public runner, with no helper patch.
            original = case.tracker.pr_head_sha

            def before_run():
                def move_after_read(pr):
                    shown = original(pr)
                    case.tracker.set_pr_head_sha(pr, changed)
                    return shown
                monkeypatch.setattr(case.tracker, "pr_head_sha", move_after_read)
    else:
        approval = case.store.load_approval(1)
        body = approval.body.replace("Implement the feature.", "Implement the approved feature and retain its receipt.")
        case.tracker.edit_body(1, body)
        case.store.save_approval(**{**approval.__dict__, "body": body, "body_sha": body_hash(body)})
    reviewed = []

    def review(kwargs):
        reviewed.append(git_command(kwargs["worktree"], "rev-parse", "HEAD"))
        return "VERDICT: PASS"

    case.reviewer.steps = [review]
    case.clock[0] = max(until, recovered)
    result = restart_at_wait_end(case, before_run)
    if change == "head-during-resume":
        if result == "merged":
            assert reviewed == [changed], "The new head must receive its own review before merge"
            assert case.tracker.pr_head_sha(case.tracker.created_prs[0].number) == changed
        else:
            assert case.tracker.merged_prs == [], "Holding changed work is also safe"
        return
    assert result == "merged"
    assert_merged_once(case)
    assert len(case.reviewer.calls) == 2, "An earlier PASS covers only its reviewed head and approved body"
