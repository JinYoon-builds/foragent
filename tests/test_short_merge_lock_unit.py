"""Short-lock edge cases beyond the locked end-to-end acceptance scenarios."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from foragent import runner
from conftest import git_command
from runner_support import enable_reviewer, run_case  # noqa: F401
from helpers.merge_lock import assert_merge_facts, assert_short_holds, run, short_case
from helpers.publication import publish_case
from helpers.home import isolated_home  # noqa: F401
from helpers.home import offline_acceptance  # noqa: F401


def test_base_already_moved_after_ci_is_refreshed_before_queuing(short_case):
    case = short_case
    original_heads = []
    moved = []

    def ci(pr, head, poll):
        if not original_heads:
            assert case.holder is None
            original_heads.append(head)
            # A source move refreshes; a neutral one keeps the head (#1688).
            case.history.update({"src/before_queue.py": "MOVED = True\n"})
            moved.append(case.history.tip)
        return "pass"

    case.ci_status = ci
    assert run(case) == "merged"
    assert len(case.holds) == 1
    locked_reads = [row for row in case.events if row.kind == "ci" and row.holder is not None]
    assert len(locked_reads) == 1 and locked_reads[0].head != original_heads[0]
    assert case.verifications == [case.base_sha, moved[0]]
    assert_short_holds(case)
    assert_merge_facts(case)


def test_ci_limits_and_registration_grace_restart_for_each_pushed_head(short_case):
    case = short_case
    implement = case.implement

    def with_workflow(kwargs):
        implement(kwargs)
        path = kwargs["worktree"] / ".github/workflows/ci.yml"
        path.parent.mkdir(parents=True)
        path.write_text("name: CI\n")

    case.implementer.steps = [with_workflow]
    first = []
    moved = []

    def move_once():
        if not moved:
            case.history.update({"src/late.py": "LATE = True\n"})  # a source move pushes a new head
            moved.append(case.history.tip)

    def ci(pr, head, poll):
        if not first:
            first.append(head)
        if head == first[0]:
            return "pending" if poll <= 40 else "pass"
        return "none" if poll <= 6 else "pass"

    case.before_lock = move_once
    case.ci_status = ci
    assert run(case) == "merged"
    assert sorted(case.poll_counts.values()) == [8, 42]
    assert case.verifications == [case.base_sha, moved[0]]
    assert_short_holds(case)
    assert_merge_facts(case)


def test_final_conflict_is_resolved_outside_the_lock(short_case):
    case = short_case
    moved = []

    def move_once():
        if not moved:
            case.history.update({"feature.txt": "theirs\n"})
            moved.append(case.history.tip)

    def resolve(kwargs):
        assert case.holder is None
        assert "<<<<<<<" in (kwargs["worktree"] / "feature.txt").read_text()
        (kwargs["worktree"] / "feature.txt").write_text("done\n")

    case.before_lock = move_once
    case.implementer.steps.append(resolve)
    case.reviewer.steps.append(case.review)  # A resolution after a PASS gets one review of that later change.
    assert run(case) == "merged"
    assert len(case.implementer.calls) == 2
    assert len(case.reviewer.calls) == 2
    assert len(case.holds) == 2
    assert_short_holds(case)
    assert_merge_facts(case)


def test_unmerged_run_keeps_both_durations_null(short_case):
    case = short_case
    moved = []

    def move_once():
        if not moved:
            case.history.update({"src/late.py": "LATE = True\n"})  # a source move pushes a new head
            moved.append(case.history.tip)

    first = []

    def ci(pr, head, poll):
        if not first:
            first.append(head)
        return "pass" if head == first[0] else "pending"

    case.before_lock = move_once
    case.ci_status = ci
    assert run(case) == "timeout"
    record = case.store.get_run(1)
    assert record.last_review_pass_at is not None
    assert record.review_pass_to_merge_seconds is None
    assert record.merge_lock_held_seconds is None
    assert len(case.holds) == 1
    assert_short_holds(case)


@pytest.mark.parametrize("verdict", [False, True])
@pytest.mark.parametrize("change", [None, "base", "policy", "approval"])
def test_reviewed_head_waits_unlocked_and_rechecks_inside_final_lock(run_case, monkeypatch, verdict, change):
    case = run_case
    case.ctx.config = replace(case.ctx.config, review="careful")
    assert runner.run_issue(1, case.ctx) == "pr-open"
    case.ctx.config = replace(case.ctx.config, review="fast")
    head = case.tracker.pr_head_sha(2)
    case.now[0] += timedelta(seconds=1)
    accepted_at = case.now[0]
    if not verdict:
        enable_reviewer(case, ["VERDICT: PASS"])
    held = []
    guards = []
    lock = case.store.merge_lock
    original_issue = case.tracker.get_issue
    merge = case.tracker.merge_pr

    @contextmanager
    def observed_lock():
        # This delay belongs to the queue and is credited to the run budget.
        case.now[0] += timedelta(seconds=19)
        with lock():
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    def issue(number):
        guards.append(bool(held))
        return original_issue(number)

    polls = []
    moved = []

    def checks(pr):
        polls.append(pr)
        return "pending" if len(polls) == 1 else "pass"

    def sleep(seconds):
        assert not held
        case.now[0] += timedelta(seconds=seconds)
        if change == "base":
            case.history.update({"docs/changed.md": "Moved base\n"})
            moved.append(case.history.tip)
        elif change == "policy":
            case.tracker.add_labels(1, ["risk:high"])
        elif change == "approval":
            case.tracker.remove_labels(1, ["approved"])

    def merge_pr(pr, **kwargs):
        assert held
        case.now[0] += timedelta(seconds=2)
        merge(pr, **kwargs)

    monkeypatch.setattr(case.store, "merge_lock", observed_lock)
    monkeypatch.setattr(case.tracker, "get_issue", issue)
    monkeypatch.setattr(case.tracker, "pr_checks", checks)
    monkeypatch.setattr(case.tracker, "merge_pr", merge_pr)
    case.ctx.sleep = sleep
    from foragent import run_review
    verified = []
    verify = run_review._verify_review_head

    def observe_verify(n, ctx, git, parsed, head_sha, link):
        verified.append(head_sha)
        return verify(n, ctx, git, parsed, head_sha, link)

    monkeypatch.setattr(run_review, "_verify_review_head", observe_verify)
    # A moved base is merged in and re-verified on the turn, not a stop (#712).
    expected = {None: "merged", "base": "merged", "policy": "held", "approval": "hash-mismatch"}[change]
    assert runner.review_only(1, case.ctx, verdict_head=head if verdict else None) == expected
    assert False in guards and True in guards
    record = case.store.get_run(1)
    assert record.last_review_pass_at == accepted_at.isoformat()
    assert record.waited_seconds == 19
    merged = change in {None, "base"}
    assert record.review_pass_to_merge_seconds == (51 if merged else None)
    assert record.merge_lock_held_seconds == (2 if merged else None)
    queue = [json.loads(line) for line in (case.store.root / "merge-queue.jsonl").read_text().splitlines()]
    review_events = [row["event"] for row in queue if row.get("started_at") == record.started_at]
    if merged:
        # A merged run leaves no withdrawn entry; the turn passes when its run stops.
        assert review_events == ["ready", "turn"]
    if change == "base":
        log = case.store.log_path(1).read_text()
        assert log.count("re-verifying") == 1, "The moved base is merged in and re-verified once"
        # The reviewed head and then the merged head both run their checks; the merge uses the new head.
        assert len(verified) == 2 and verified[0] == head and verified[1] != head
        assert case.tracker.pr_head_sha(2) == verified[1]
        assert git_command(case.history.origin, "merge-base", "--is-ancestor", moved[-1], verified[1]) == ""
        assert case.tracker.merged_prs == [2]
    else:
        assert verified == [head]


def test_ci_repair_latency_uses_the_last_reviewer_pass(run_case, monkeypatch):
    case = run_case
    case.executor.steps.append({})
    passes = []

    def review_time(worktree):
        case.now[0] += timedelta(seconds=13)
        passes.append(case.now[0])

    enable_reviewer(case, ["VERDICT: PASS", "VERDICT: PASS"], edit=review_time)
    checks = iter(["fail", "pass"])
    monkeypatch.setattr(case.tracker, "pr_checks", lambda pr: next(checks, "pass"))
    merge = case.tracker.merge_pr

    def merge_pr(pr, **kwargs):
        case.now[0] += timedelta(seconds=5)
        merge(pr, **kwargs)

    monkeypatch.setattr(case.tracker, "merge_pr", merge_pr)
    assert runner.run_issue(1, case.ctx) == "merged"
    record = case.store.get_run(1)
    assert len(passes) == 2
    assert record.last_review_pass_at == passes[-1].isoformat()
    assert record.review_pass_to_merge_seconds == 5
    assert record.merge_lock_held_seconds == 5


def test_lock_hold_includes_bookkeeping_after_acquisition(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from conftest import BASE_TIME
    from foragent import run_publish
    from foragent.state import StateStore

    clock = [BASE_TIME]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.start_run(1, branch="branch", worktree="worktree")
    ctx = SimpleNamespace(store=store, now=lambda: clock[0], git=SimpleNamespace(root=tmp_path / "checkout"))

    def credited_wait(number, context, acquire, phase, **options):
        acquire()
        clock[0] += timedelta(seconds=3)

    monkeypatch.setattr(run_publish, "_credited_wait", credited_wait)
    with run_publish._merge_lock(1, ctx):
        clock[0] += timedelta(seconds=2)
        store.record_merge(1, clock[0])
    assert store.get_run(1).merge_lock_held_seconds == 5


@pytest.mark.parametrize("path", ["implementation", "review-only"])
def test_merge_finalization_stays_inside_the_final_lock(run_case, monkeypatch, path):
    case = run_case
    if path == "review-only":
        case.ctx.config = replace(case.ctx.config, review="careful")
        assert runner.run_issue(1, case.ctx) == "pr-open"
        case.ctx.config = replace(case.ctx.config, review="fast")
        case.now[0] += timedelta(seconds=1)
    held, events = [], []
    lock = case.store.merge_lock
    close = case.tracker.close_issue

    @contextmanager
    def observed_lock():
        with lock():
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    def after_merge(*args):
        assert held
        events.append("reinstall")

    def close_issue(*args):
        assert held
        events.append("close")
        return close(*args)

    monkeypatch.setattr(case.store, "merge_lock", observed_lock)
    monkeypatch.setattr(runner, "after_merge", after_merge)
    monkeypatch.setattr(case.tracker, "close_issue", close_issue)
    outcome = (runner.review_only(1, case.ctx, verdict_head=case.tracker.pr_head_sha(2))
               if path == "review-only" else runner.run_issue(1, case.ctx))
    assert outcome == "merged"
    assert events == ["reinstall", "close"]
    assert not held
