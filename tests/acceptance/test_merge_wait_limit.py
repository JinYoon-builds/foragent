"""Locked acceptance for merge-wait-limit; drive run_issue with offline I/O.

Queue, wait accounting, cleanup, GitHub error handling and publication stay real.
Only time, Git transport, executors and remote facts are faked. No future helper
or another draft is needed to collect this file on main.
"""

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
import json
import socket

import pytest

from conftest import git_command
from runner_support import run_case  # noqa: F401
from foragent import orch_events, orch_inbox, run_merge_turn as turn
from foragent.issue import body_hash
from foragent.overlap_files import claims
from foragent.runner import run_issue
from foragent.state import StateStore
from foragent.tracker import RecordingTracker, TrackerError


LIMIT = 57_600
INTERVAL = 300


class WaitFacts:
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
    case.facts = WaitFacts(case)
    case.ctx.tracker = RecordingTracker(case.facts, case.ctx)
    merge = case.tracker.merge_pr

    def merge_once(pr, **kwargs):
        case.merge_calls.append(pr)
        assert case.store.get_run(2).status == "finished", "The predecessor still owns the turn"
        return merge(pr, **kwargs)

    def sleep(seconds):
        assert 0 < seconds <= INTERVAL, "The wait loop must revisit facts within five minutes"
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


def release_predecessor(case):
    if case.store.get_run(2).status == "running":
        case.store.finish_run(2, outcome="merged")
        case.tracker.close_issue(2)
        case.tracker.remove_labels(2, ["running"])


def exercise(case, *, release_at=900):
    during = case.during_wait

    def advance():
        during()
        if case.waited >= release_at:
            release_predecessor(case)

    case.during_wait = advance
    try:
        return run_issue(1, case.ctx)
    finally:
        release_predecessor(case)


def terminal_fact(case, kind):
    if kind == "issue-closed":
        case.tracker.close_issue(1)
    else:
        # Model an external actor, without calling the run's merge transport.
        case.tracker.issues[3] = replace(case.tracker.issues[3], state="closed")
        if kind == "pr-merged":
            case.tracker.created_prs = [
                replace(pr, merged_at=case.now[0].isoformat()) if pr.number == 3 else pr
                for pr in case.tracker.created_prs
            ]


def assert_released(case):
    store = StateStore(case.store.root, now=lambda: case.now[0])
    run = store.get_run(1)
    assert run.status == "finished"
    assert run.waiting_until is None and run.pause_regions == {}
    known, unknown = claims(case.ctx, measured=False)
    assert 1 not in {c.run.number for c in known} | {r.number for r in unknown}
    rows = [json.loads(line) for line in (store.root / "merge-queue.jsonl").read_text().splitlines()]
    own = [r for r in rows if r["n"] == 1 and r.get("started_at") == run.started_at]
    assert [r["event"] for r in own] == ["ready", "withdrawn"]
    assert case.merge_calls == [], "An ended remote issue/PR must never reach merge_pr"


def assert_normal_stop(case):
    assert_released(case)
    assert 330 <= case.waited <= 630, "A terminal fact at t=330 must be seen by the next five-minute check"
    assert case.store.load_flag(1) is None
    assert not case.tracker.get_issue(1).labels & {"running", "blocked", "needs-human"}
    assert orch_inbox.requests(case.store, pending=True) == []
    assert orch_events.events(case.store, "merge-wait-limit") == []


def assert_cadence(case):
    for family in ("issue", "pr"):
        times = [t for f, t, _ in case.facts.reads if f == family]
        assert times, f"The wait loop never read {family} facts"
        assert times[0] <= INTERVAL
        assert all(b - a == INTERVAL for a, b in zip(times, times[1:])), times
        assert case.waited - times[-1] <= INTERVAL, times
        # No duplicate call hidden inside one round and no retries before due.
        assert max(Counter(times).values()) == 1


@pytest.mark.parametrize("kind", ["issue-closed", "pr-merged", "pr-closed"])
def test_terminal_remote_fact_stops_and_releases_the_waiting_run(waiting_case, kind):
    case = waiting_case
    case.during_wait = lambda: terminal_fact(case, kind) if case.waited >= 330 else None
    exercise(case)
    assert_normal_stop(case)
    assert case.tracker.issues[3].state == ("open" if kind == "issue-closed" else "closed")
    assert case.tracker.issues[1].state == ("closed" if kind == "issue-closed" else "open")


@pytest.mark.parametrize("family", ["issue", "pr"])
def test_failed_read_keeps_waiting_and_a_later_check_can_stop(waiting_case, family):
    case = waiting_case
    case.facts.fail_family = family
    case.facts.error = TrackerError("HTTP 502: temporary GitHub read failure")
    case.during_wait = lambda: terminal_fact(case, "issue-closed" if family == "issue" else "pr-merged") \
        if case.waited >= 330 else None
    exercise(case)
    assert len(case.facts.failed) == 1, "The injected read failure was never exercised"
    failed_at = case.facts.failed[0][1]
    retries = [t for f, t, _ in case.facts.reads if f == family and t > failed_at]
    assert retries and retries[0] - failed_at == INTERVAL
    for fact in ("issue", "pr"):
        times = [t for f, t, _ in case.facts.reads if f == fact]
        assert all(b - a >= INTERVAL for a, b in zip(times, times[1:])), times
    assert_normal_stop(case)


@pytest.mark.parametrize("rejoin", [False, True], ids=["one-wait", "rejoined"])
def test_healthy_wait_reads_each_fact_once_per_five_minutes(waiting_case, monkeypatch, rejoin):
    case = waiting_case
    if rejoin:
        merge = case.tracker.merge_pr
        refused = []

        def refuse_once(pr, **kwargs):
            if not refused:
                refused.append(pr)
                case.merge_calls.append(pr)
                case.tracker.add_issue(4, "Next ready run", labels=["running"])
                case.store.start_run(4, branch="foragent/issue-4", worktree=case.store.worktree_path(4))
                turn.join(case.ctx, turn.seat(case.ctx, 4, "main"), case.ctx.git.ref_sha("origin/main"))
                raise TrackerError("Pull request is not mergeable right now")
            return merge(pr, **kwargs)

        def finish_four():
            peer = case.store.get_run(4)
            if peer is not None and peer.status == "running":
                case.store.finish_run(4, outcome="merged")
                case.tracker.close_issue(4)
                case.tracker.remove_labels(4, ["running"])

        case.during_wait = lambda: finish_four() if case.waited >= 930 else None
        monkeypatch.setattr(case.tracker, "merge_pr", refuse_once)
        try:
            outcome = exercise(case, release_at=330)
        finally:
            finish_four()
    else:
        outcome = exercise(case, release_at=930)
    assert outcome == "merged" and case.tracker.merged_prs == [3]
    assert case.merge_calls == ([3, 3] if rejoin else [3])
    assert len(case.executor.calls) == 1
    assert git_command(case.history.origin, "show", f"{case.tracker.pr_head_sha(3)}:feature.txt") == "done"
    assert case.waited >= 930
    assert_cadence(case)
    assert orch_inbox.requests(case.store, pending=True) == []
    assert orch_events.events(case.store, "merge-wait-limit") == []


@pytest.mark.parametrize("status", [403, 429])
def test_probe_rate_limit_uses_the_existing_resumable_wait(waiting_case, status):
    case = waiting_case
    reset = case.now[0] + timedelta(hours=1)
    case.facts.fail_family = "issue"
    case.facts.error = TrackerError(f"HTTP {status}: API rate limit exceeded", stdout=(
        f"x-ratelimit-reset: {int(reset.timestamp())}\n"
    ))
    outcome = exercise(case)
    assert len(case.facts.failed) == 1, "The rate-limited merge-wait read was never made"
    assert outcome == "limit-wait"
    store = StateStore(case.store.root, now=lambda: case.now[0])
    assert datetime.fromisoformat(store.get_run(1).waiting_until) == reset
    assert store.github_wait_until(case.now[0]) == reset
    assert store.load_flag(1) is None
    assert not case.tracker.issues[1].labels & {"blocked", "needs-human"}
    assert case.merge_calls == [] and case.waited <= INTERVAL
    assert orch_inbox.requests(store, pending=True) == []
    assert orch_events.events(store, "merge-wait-limit") == []


@pytest.mark.parametrize("already_waited", [LIMIT - 60, LIMIT], ids=["last-minute", "already-exhausted"])
def test_cumulative_limit_is_sixteen_hours_and_emits_one_countable_hit(waiting_case, already_waited):
    case = waiting_case
    # Existing run accounting represents earlier waits in this same run. This
    # must not reset on entering a new wait; no production budget is patched.
    case.store.update_run(1, merge_waited_seconds=already_waited)
    outcome = exercise(case)
    assert outcome == "merge-wait"
    assert case.waited == LIMIT - already_waited, (
        "Stop at cumulative 57,600 seconds, neither 3,600 nor a fresh 16 hours"
    )
    assert_released(case)
    store = StateStore(case.store.root, now=lambda: case.now[0])
    run = store.get_run(1)
    requests = orch_inbox.requests(store, pending=True)
    assert len(requests) == 1 and requests[0]["kind"] == "merge-wait"
    assert requests[0]["issue"] == 1
    assert requests[0]["run_started_at"] == run.started_at
    assert requests[0]["waited_seconds"] == LIMIT
    assert requests[0]["polls"] == run.merge_wait_polls == len(case.sleeps)
    hits = orch_events.events(store, "merge-wait-limit")
    assert len(hits) == 1, "Each limit hit needs one measurable journal event in addition to the inbox"
    hit = hits[0]
    assert hit["issue"] == 1 and hit["run_started_at"] == run.started_at
    assert hit["waited_seconds"] == LIMIT and hit["limit_seconds"] == LIMIT
    assert datetime.fromisoformat(hit["at"]) == case.now[0]
    turn.withdraw(case.ctx, turn.seat(case.ctx, 1, "main"))
    assert orch_events.events(store, "merge-wait-limit") == hits, "Cleanup must not count the hit twice"
    assert "blocked" in case.tracker.issues[1].labels
    assert case.tracker.issues[3].state == "open" and len(case.tracker.created_prs) == 1
