"""Shared offline helpers for unit tests."""

from dataclasses import replace
from datetime import datetime, timedelta
import socket
import subprocess

import pytest

from foragent import run_merge_turn as turn
from foragent.issue import body_hash
from foragent.overlap_files import claims
from foragent.runner import run_issue
from foragent.scheduler import tick
from foragent.state import StateStore
from foragent.tracker import GhTracker, RecordingTracker
from runner_support import run_case

from helpers.review_case import make_case, tick_context


_INTERVAL = 300


class _WaitFacts:
    """Observe external tracker reads, excluding FakeTracker's nested calls.

    One round may read both the issue and its known PR. Counting each family
    separately permits either existing single-object or listing tracker APIs.
    Failed reads consume their interval too. Fixture writes bypass this proxy.
    """

    def __init__(self, case):
        self.case = case
        self.reads = []
        self.fail_family = None
        self.error = None
        self.failed = []

    def __getattr__(self, name):
        value = getattr(self.case.tracker, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            case = self.case
            family = None
            if case.store.get_run(1).phase == "merge-lock-wait":
                if name == "get_issue":
                    family = "issue" if args[0] == 1 else "pr"
                elif name in {"list_open_issues", "list_issues"}:
                    family = "issue"
                elif name in {"find_pr", "list_prs", "pr_head_sha"}:
                    family = "pr"
                elif name == "pr_checks":
                    pytest.fail("Waiting for a turn must not poll CI")
            if family:
                row = (family, case.waited, name)
                self.reads.append(row)
                if self.fail_family == family and not self.failed:
                    self.failed.append(row)
                    raise self.error
            return value(*args, **kwargs)

        return call


@pytest.fixture
def waiting_case(run_case, monkeypatch):
    case = run_case
    case.ctx.config = replace(case.ctx.config, max_runtime_minutes=1,
                              max_retries=0, skill_libraries=[], merge_group_max=1)
    case.ctx.poll_hook = lambda execute, poll: execute()
    approval = case.store.load_approval(1)
    body = approval.body + "\n## Files\n- feature.txt\n"
    case.tracker.update_issue(1, title="Implement feature", body=body)
    case.store.save_approval(1, body=body, body_sha=body_hash(body),
                             acceptance_blobs=approval.acceptance_blobs,
                             approved_by="human", approved_at=case.now[0].isoformat())
    case.store.update_run(1, approval_body_sha=body_hash(body))
    case.tracker.add_issue(2, "Earlier ready run", labels=["running"])
    case.store.start_run(2, branch="foragent/issue-2", worktree=case.store.worktree_path(2))
    turn.join(case.ctx, turn.seat(case.ctx, 2, "main"), case.ctx.git.ref_sha("origin/main"))
    case.tracker.set_pr_files(3, ["feature.txt"])
    case.tracker.set_pr_checks(3, "pass")
    case.waited = 0
    case.sleeps = []
    case.merge_calls = []
    case.during_wait = lambda: None
    case.facts = _WaitFacts(case)
    case.ctx.tracker = RecordingTracker(case.facts, case.ctx)
    merge = case.tracker.merge_pr

    def merge_once(pr, **kwargs):
        case.merge_calls.append(pr)
        assert case.store.get_run(2).status == "finished", "The predecessor still owns the turn"
        return merge(pr, **kwargs)

    def sleep(seconds):
        assert 0 < seconds <= _INTERVAL, "The wait loop must revisit facts within five minutes"
        assert case.waited < 2_100, "Offline scenario did not finish at its released turn"
        assert case.store.get_run(1).phase == "merge-lock-wait"
        known, _ = claims(case.ctx, measured=False)
        assert any(c.run.number == 1 and "feature.txt" in c.files for c in known), (
            "This scenario must hold a real approval-backed file claim while waiting"
        )
        assert len(case.executor.calls) == 1
        case.sleeps.append(seconds)
        case.waited += seconds
        case.now[0] += timedelta(seconds=seconds)
        case.during_wait()

    def no_network(*args, **kwargs):
        raise AssertionError("This acceptance test must stay offline")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(turn, "monotonic", case.ctx.monotonic)
    monkeypatch.setattr(case.tracker, "merge_pr", merge_once)
    case.ctx.sleep = sleep
    return case


def _release_predecessor(case):
    if case.store.get_run(2).status == "running":
        case.store.finish_run(2, outcome="merged")
        case.tracker.close_issue(2)
        case.tracker.remove_labels(2, ["running"])


def exercise(case, *, release_at=900):
    during = case.during_wait

    def advance():
        during()
        if case.waited >= release_at:
            _release_predecessor(case)

    case.during_wait = advance
    try:
        return run_issue(1, case.ctx)
    finally:
        _release_predecessor(case)


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
