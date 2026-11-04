"""Shared offline helpers for unit tests."""

from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import run_checks, runner

from helpers.publication import publish_case, squash_main


@pytest.fixture
def short_case(publish_case, monkeypatch):
    """Reuse make_case via #376's real Git/checks/baseline fixture.

    Only tracker CI, model executors and time are fake. Observe the public
    StateStore lock, not _publish_locked or a replacement merge algorithm.
    A nested contender fails immediately instead of deadlocking this offline
    test; otherwise the wrapper acquires the real project's merge.lock.
    """
    case = publish_case
    case.active = 1
    case.holder = None
    case.holds, case.events, case.merges = [], [], []
    case.review_passes, case.green_heads = {}, {}
    case.poll_counts = Counter()
    case.before_lock = lambda: None
    case.during_sleep = lambda: None
    case.ci_status = lambda pr, head, poll: "pass"

    def advance(seconds):
        case.clock[0] += timedelta(seconds=seconds)

    def event(kind, **fields):
        row = SimpleNamespace(kind=kind, issue=case.active, at=case.clock[0],
                              holder=case.holder, **fields)
        case.events.append(row)
        return row

    merge_lock = case.store.merge_lock

    @contextmanager
    def observed_lock():
        event("lock-request")
        case.before_lock()
        assert case.holder is None, (
            f"Run {case.active} cannot acquire merge.lock: run {case.holder} "
            "still holds it while waiting for CI"
        )
        with merge_lock():
            pr = case.tracker.find_pr(f"foragent/issue-{case.active}")
            head = case.tracker.pr_head_sha(pr) if pr is not None else None
            hold = SimpleNamespace(
                issue=case.active, start=case.clock[0], end=None,
                green=head is not None and case.green_heads.get(pr) == head,
            )
            case.holds.append(hold)
            case.holder = case.active
            event("lock-acquired")
            try:
                yield
            finally:
                hold.end = case.clock[0]
                event("lock-released")
                case.holder = None

    verify = runner._verify_implementation

    def observe_verification(attempt):
        event("verify", base=attempt.git.ref_sha("origin/main"))
        advance(7)
        return verify(attempt)

    def review(kwargs):
        advance(11)
        case.review_passes[case.active] = case.clock[0]
        return "VERDICT: PASS"

    checks = case.tracker.pr_checks

    def poll(pr):
        head = case.tracker.pr_head_sha(pr)
        case.poll_counts[pr, head] += 1
        status = case.ci_status(pr, head, case.poll_counts[pr, head])
        case.tracker.set_pr_checks(pr, status)
        if status == "pass":
            case.green_heads[pr] = head
        event("ci", pr=pr, head=head, status=checks(pr))
        return checks(pr)

    def sleep(seconds):
        event("sleep", seconds=seconds)
        advance(seconds)
        case.during_sleep()

    merge = case.tracker.merge_pr

    def merge_into_main(pr, *, expected_head_sha, method="squash", expected_base_sha=None):
        assert case.holder == case.active, "The final merge must hold merge.lock"
        assert case.green_heads.get(pr) == expected_head_sha, "CI must pass on the merged head"
        assert case.tracker.pr_head_sha(pr) == expected_head_sha
        assert checks(pr) == "pass", "A previous head's green CI cannot authorize this merge"
        advance(2)
        merge(pr, expected_head_sha=expected_head_sha, method=method,
              **({"expected_base_sha": expected_base_sha} if expected_base_sha is not None else {}))
        # FakeTracker records GitHub metadata; also advance the real offline
        # origin so the other runner must actually refresh its base.
        squash_main(case.history, expected_head_sha)
        case.merges.append(event("merge", pr=pr, head=expected_head_sha))

    case.advance = advance
    case.ctx.sleep = sleep
    case.reviewer.steps = [review]
    case.review = review
    monkeypatch.setattr(case.store, "merge_lock", observed_lock)
    monkeypatch.setattr(runner, "_verify_implementation", observe_verification)
    monkeypatch.setattr(run_checks, "_verify_implementation", observe_verification)
    monkeypatch.setattr(case.tracker, "pr_checks", poll)
    monkeypatch.setattr(case.tracker, "merge_pr", merge_into_main)
    return case


def run(case, number=1):
    previous, case.active = case.active, number
    try:
        return runner.run_issue(number, case.ctx)
    finally:
        case.active = previous


def _slow_under_lock(case):
    """Work that must never happen while merge.lock is held.

    Verification and sleeping are always slow. Inside one hold, the final
    decision may re-read CI status of the exact head once (a non-waiting
    read, so a rerun that failed during the lock wait is seen); a second CI
    read in the same hold is polling.
    """
    slow, reads, hold = [], 0, None
    for row in case.events:
        if row.kind == "lock-acquired":
            reads, hold = 0, row
        elif row.kind == "lock-released":
            hold = None
        elif row.holder is None:
            continue
        elif row.kind in {"verify", "sleep"}:
            slow.append(row)
        elif row.kind == "ci":
            reads += 1
            if reads > 1:
                slow.append(row)
    return slow


def assert_short_holds(case):
    assert case.holder is None
    slow = _slow_under_lock(case)
    assert not slow, (
        "Premerge verification and CI polling/waiting must run outside merge.lock "
        "(one non-waiting CI status read per hold is allowed); "
        f"observed {[(row.kind, row.issue, row.holder) for row in slow]}"
    )
    if case.merges:
        assert case.holds, "Successful publication must enter StateStore.merge_lock()"
    for hold in case.holds:
        assert hold.end is not None
        assert hold.green, "Acquire the final lock only after CI passed on the current published head"
        merges = [row for row in case.merges
                  if row.issue == hold.issue and hold.start <= row.at <= hold.end]
        assert (hold.end - hold.start).total_seconds() == 2 * len(merges), (
            "Only final base validation and merge belong inside merge.lock"
        )


def assert_merge_facts(case, number=1):
    """Chosen schema: duration fields on the persisted per-run record.

    Read disk afresh, so an in-memory metric or a pre-seeded fixture cannot
    satisfy the producer contract needed by fa orch facts (#333).
    """
    saved = json.loads((case.store.root / "runs" / f"{number}.json").read_text())
    fields = {"review_pass_to_merge_seconds", "merge_lock_held_seconds"}
    assert fields <= saved.keys(), f"Merge facts missing from run {number}: {fields - saved.keys()}"
    merged, = [row for row in case.merges if row.issue == number]
    elapsed = (merged.at - case.review_passes[number]).total_seconds()
    held = sum((hold.end - hold.start).total_seconds()
               for hold in case.holds if hold.issue == number)
    assert type(saved["review_pass_to_merge_seconds"]) in (int, float)
    assert type(saved["merge_lock_held_seconds"]) in (int, float)
    assert saved["review_pass_to_merge_seconds"] == pytest.approx(elapsed)
    assert saved["merge_lock_held_seconds"] == pytest.approx(held)
    assert 0 < held <= elapsed
