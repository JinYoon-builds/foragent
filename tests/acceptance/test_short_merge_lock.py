"""Acceptance test for short-merge-lock. Locked by Foragent; do not edit."""

from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import run_checks, runner, scheduler, scheduler_recovery
from foragent.issue import body_hash
from test_base_move_reuse import (  # noqa: F401
    MERGED, PENDING, SHARED_DOC, SHARED_SOURCE, UNOWNED,
    locked_test, publish_case,
)
from test_orch_review_handoff import tick_context
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


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
        git_command(case.history.origin, "update-ref", "refs/heads/main", expected_head_sha)
        case.history.tip = expected_head_sha
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


def assert_merged(case, number=1):
    record = case.store.get_run(number)
    assert record.outcome == "merged", case.store.log_path(number).read_text()
    assert case.tracker.get_issue(number).state == "closed"
    assert len([row for row in case.merges if row.issue == number]) == 1


def slow_under_lock(case):
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
    slow = slow_under_lock(case)
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


def move_at_lock(case, batches):
    """Another merger advances main while A queues for its final lock.

    Inject at the public acquisition boundary, after all work the caller chose
    to do beforehand. The old broad lock instead exposes its early acquisition
    and any ensuing verification/CI inside the observed hold.
    """
    case.moves = []

    def advance_main():
        if case.moves:
            return
        for files in batches:
            case.history.update(files)
        case.moves.append(case.history.tip)

    case.before_lock = advance_main
    # Every pushed head, including the refreshed head, needs its own green CI.
    case.ci_status = lambda pr, head, poll: "pending" if poll <= 2 else "pass"


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


def test_pending_ci_allows_another_ready_run_to_merge_first(short_case, monkeypatch):
    case = short_case
    # Use tick_context's fake launcher to start a ready run in another lane;
    # invoke its fake-executor runner cooperatively during A's fake CI sleep.
    case.ctx.config = replace(case.ctx.config, lanes={"other": {}})
    approval = case.store.load_approval(1)
    body = approval.body.replace("feature.txt", "feature-b.txt")
    case.tracker.add_issue(4, "Independent feature", body, labels=["ready", "lane:other"])
    case.tracker.record_label(4, "approved", "human")
    case.store.save_approval(
        4, body=body, body_sha=body_hash(body), acceptance_blobs=approval.acceptance_blobs,
        approved_by="human", approved_at=case.clock[0].isoformat(),
    )
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.1))
    # run_issue journals its own PID; tick's liveness check must stay offline,
    # including signal-0 probes, just like tick_context's launcher/process seams.
    monkeypatch.setattr(scheduler_recovery, "_pid_exists", lambda pid: True)

    def implement_b(kwargs):
        (kwargs["worktree"] / "feature-b.txt").write_text("independent\n")
        return "Implemented B"

    case.implementer.steps.append(implement_b)
    case.reviewer.steps.append(case.review)
    case.ci_status = lambda pr, head, poll: (
        "pending" if case.active == 1 and poll <= 6 else "pass"
    )
    launched = []

    def run_b():
        if launched:
            return
        report = scheduler.tick(tick_context(case, baseline_launcher=lambda ctx, build, lock: lock.close()))
        assert report.skipped is None
        assert case.store.get_run(4) is not None, report.actions
        launched.append(run(case, 4))

    case.during_sleep = run_b
    outcome = run(case)
    assert outcome == "merged", case.store.log_path(1).read_text()
    assert launched == ["merged"], (
        f"B must merge while A's CI is still pending: {case.store.get_run(4).detail}"
    )
    assert_merged(case, 4)
    assert_merged(case)
    assert [row.issue for row in case.merges] == [4, 1]
    first_green = next(row.at for row in case.events
                       if row.kind == "ci" and row.issue == 1 and row.status == "pass")
    assert case.merges[0].at < first_green
    assert len([row for row in case.events
                if row.kind == "ci" and row.issue == 1 and row.status == "pending"]) >= 6
    assert_short_holds(case)
    assert_merge_facts(case, 4)
    assert_merge_facts(case)


def test_premerge_reverification_runs_outside_merge_lock(short_case):
    case = short_case
    moved = []

    def review_then_move(kwargs):
        verdict = case.review(kwargs)
        case.history.update({"src/widget.py": "VALUE = 1\n"})
        moved.append(case.history.tip)
        return verdict

    case.reviewer.steps = [review_then_move]
    assert run(case) == "merged"
    assert len(moved) == 1
    assert case.verifications == [case.base_sha, moved[0]]
    assert_merged(case)
    assert_short_holds(case)


@pytest.mark.parametrize("batches", [
    pytest.param([{"docs/guide.md": "Unrelated documentation\n"}], id="docs"),
    pytest.param([{PENDING: locked_test("pending", passing=False) + "\n# Clarified spec\n"}],
                 id="excluded-locked"),
    pytest.param([{"docs/guide.md": "Documentation\n"},
                  {PENDING: locked_test("pending", passing=False) + "\n# Clarified spec\n"}],
                 id="docs-and-excluded-locked"),
])
def test_final_base_move_reuses_neutral_checks_with_short_lock(short_case, batches):
    case = short_case
    move_at_lock(case, batches)
    assert run(case) == "merged"
    assert len(case.moves) == 1
    assert case.verifications == [case.base_sha], "Reuse #376's actual baseline exclusion classification"
    git_command(case.history.origin, "merge-base", "--is-ancestor", case.moves[0], case.history.tip)
    assert len(case.implementer.calls) == len(case.reviewer.calls) == 1
    assert_merged(case)
    assert_short_holds(case)


@pytest.mark.parametrize("batches", [
    pytest.param([{"src/widget.py": "VALUE = 1\n"}], id="source"),
    pytest.param([{"tests/test_widget.py": "def test_widget():\n    assert 1 == 1\n"}], id="unit-test"),
    pytest.param([{".foragent.yml": "repo: owner/repo\nreview: careful\n"}], id="config"),
    pytest.param([{"pyproject.toml": '[project]\nname = "offline-feature"\nversion = "0.1.0"\n'}],
                 id="pyproject"),
    pytest.param([{MERGED: locked_test("merged") + "\n# Merged owner\n"}], id="merged-locked"),
    pytest.param([{UNOWNED: locked_test("unowned") + "\n# Unowned\n"}], id="unowned-locked"),
    pytest.param([{"docs-notes.md": "Outside docs/\n"}], id="docs-prefix"),
    pytest.param([{"docs/guide.md": "Docs\n"}, {"src/widget.py": "VALUE = 1\n"}], id="mixed"),
    pytest.param([{"docs/guide.md": "Docs\n"}, {"src/widget.py": "VALUE = 1\n"},
                  {"src/widget.py": "VALUE = 0\n"}], id="source-reverted-in-batch"),
])
def test_final_affecting_move_releases_lock_rechecks_and_queues_again(short_case, batches):
    case = short_case
    move_at_lock(case, batches)
    assert run(case) == "merged"
    assert len(case.moves) == 1
    assert case.verifications == [case.base_sha, case.moves[0]], "Exactly one recheck for the fetched batch"
    git_command(case.history.origin, "merge-base", "--is-ancestor", case.moves[0], case.history.tip)
    assert_merged(case)
    assert_short_holds(case)
    recheck, = [row for row in case.events if row.kind == "verify" and row.base == case.moves[0]]
    released = [row for row in case.events if row.kind == "lock-released" and row.at <= recheck.at]
    reacquired = [row for row in case.events if row.kind == "lock-acquired" and row.at > recheck.at]
    assert released and reacquired, "Release the final lock before rechecking, then queue again"
    assert len(case.implementer.calls) == len(case.reviewer.calls) == 1


def test_final_move_to_prs_own_doc_rechecks_outside_lock(short_case):
    case = short_case

    def implement(kwargs):
        case.implement(kwargs)
        (kwargs["worktree"] / SHARED_DOC).write_text(SHARED_SOURCE.replace("PR section", "PR updated"))

    case.implementer.steps = [implement]
    move_at_lock(case, [{SHARED_DOC: SHARED_SOURCE.replace("Main section", "Main updated")}])
    assert run(case) == "merged"
    assert case.verifications == [case.base_sha, case.moves[0]], "An overlapping doc is result-affecting (#376)"
    published = git_command(case.history.origin, "show", f"main:{SHARED_DOC}")
    assert "PR updated" in published and "Main updated" in published
    assert_merged(case)
    assert_short_holds(case)


@pytest.mark.parametrize("status,expected", [("pending", "timeout"), ("fail", "ci-failed")])
def test_refreshed_head_never_merges_using_previous_heads_green_ci(short_case, status, expected):
    case = short_case
    first_head = []

    def ci(pr, head, poll):
        if not first_head:
            first_head.append(head)
            # Move after the original head's green response, before final merge.
            case.history.update({"src/widget.py": "VALUE = 1\n"})
        return "pass" if head == first_head[0] else status

    case.ci_status = ci
    assert run(case) == expected
    polls = [row for row in case.events if row.kind == "ci"]
    assert len({row.head for row in polls}) == 2, "The base merge must publish a new head"
    assert any(row.status == status and row.head != first_head[0] for row in polls)
    assert case.merges == case.tracker.merged_prs == []
    assert case.tracker.get_issue(1).state == "open"
    assert_short_holds(case)


def test_changed_pr_head_is_held_without_waiting_under_lock(short_case):
    case = short_case

    def ci(pr, head, poll):
        if poll < 3:
            return "pending"
        case.tracker.set_pr_head_sha(pr, "f" * 40)
        return "pass"

    case.ci_status = ci
    assert run(case) == "held"
    assert case.merges == case.tracker.merged_prs == []
    assert case.tracker.get_issue(1).state == "open"
    # A short-lock implementation need not acquire at all for a rejected head.
    assert not slow_under_lock(case), (
        "Rejecting a changed head must not monopolize merge.lock during CI"
    )


@pytest.mark.parametrize("pending_polls,queue_seconds", [(0, 13), (5, 37)])
def test_merge_records_review_latency_and_lock_hold_from_fake_clock(short_case, pending_polls, queue_seconds):
    case = short_case
    case.ci_status = lambda pr, head, poll: "pending" if poll <= pending_polls else "pass"
    # Time queued precedes acquiring the real lock and must affect latency,
    # but never the recorded hold time. The 7 s check and 11 s review precede PASS.
    case.before_lock = lambda: case.advance(queue_seconds)
    assert run(case) == "merged"
    assert_merged(case)
    assert_merge_facts(case)
    merged, = case.merges
    assert (merged.at - case.review_passes[1]).total_seconds() == pending_polls * 30 + queue_seconds + 2
    assert_short_holds(case)


def test_ci_rerun_failing_during_lock_wait_prevents_the_merge(short_case):
    case = short_case

    def rerun_fails():
        # CI passed on this head, then a rerun of the same head failed while
        # the run queued for merge.lock; the final decision must see it.
        case.ci_status = lambda pr, head, poll: "fail"

    case.before_lock = rerun_fails
    assert run(case) != "merged"
    assert case.merges == case.tracker.merged_prs == []
    assert case.tracker.get_issue(1).state == "open"
    assert_short_holds(case)


def test_risk_high_added_during_lock_wait_holds_the_merge(short_case):
    case = short_case

    def mark_risky():
        if "risk:high" not in case.tracker.get_issue(1).labels:
            case.tracker.add_labels(1, ["risk:high"])

    case.before_lock = mark_risky
    assert run(case) == "held"
    assert case.merges == case.tracker.merged_prs == []
    assert case.tracker.get_issue(1).state == "open"
    assert_short_holds(case)
