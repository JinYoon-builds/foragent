"""Architect r992 (2026-11-07): a red train's innocent remainder rejoins batching.

Uses the existing real Git / state / measured-CI merge-owner world and drives
it only through the public owner tick (support.gate). A = #5, B = #1, and
C = #4, D = #2 are queued while the retry [A] is running.
"""

from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from helpers.merge_world import world  # noqa: E402, F401
from test_merge_owner_proto import proto  # noqa: E402, F401
import test_merge_owner_proto as support  # noqa: E402
from test_merge_owner_batch import (  # noqa: E402, F401
    batched, composed_numbers, events, green, groups, queue_workers, refs,
)
from conftest import git_command  # noqa: E402
from foragent import merge_owner, merge_owner_batch as batch, orch_queue  # noqa: E402
from foragent.state import StateStore  # noqa: E402

A, B, C, D = 5, 1, 4, 2


def red_train_then_retry(case):
    """[A, B] goes red; the retry [A] is published; C and D queue meanwhile."""
    case.pending_batch = True
    queue_workers(case, [A, B])
    orch_queue.set_global(case.store, [A, B])
    support.gate(case)
    assert composed_numbers(case) == [A, B]
    case.tracker.set_pr_checks(groups(case)[-1].number, "fail")
    support.gate(case)
    assert not refs(case) and events(case)[-1]["reason"] == "tip CI red"
    support.gate(case)
    assert composed_numbers(case) == [A], "D120 still retries the first half alone"
    assert len(groups(case)) == 2
    queue_workers(case, [C, D])
    orch_queue.set_global(case.store, [A, B, C, D])
    support.gate(case)  # The pending retry keeps its reservation; C and D wait.
    assert len(groups(case)) == 2 and composed_numbers(case) == [A]
    assert not case.target_pushes and not case.merges


def test_spent_single_half_lets_remainder_join_waiting_members(batched):
    case = batched
    red_train_then_retry(case)
    case.tracker.set_pr_checks(groups(case)[-1].number, "fail")
    support.gate(case)
    assert events(case)[-1]["reason"] == "tip CI red"
    spent = [m["n"] for row in batch.retirements(case.ctx) for m in row["spent"]]
    assert spent == [A], "The red single-member half is spent exactly once"
    assert not case.target_pushes and not case.merges

    support.gate(case)
    trains = groups(case)
    assert len(trains) == 3, f"exactly one new train expected, got {[pr.head for pr in trains]}"
    assert refs(case)
    assert composed_numbers(case) == [B, C, D], (
        "After a spent single-member half, the remainder must join normal batching "
        f"in queue order instead of a solo reservation; next train was {composed_numbers(case)}"
    )
    assert not case.target_pushes and not case.merges
    # A stays on the single path; it is still an admission but not a train member.
    assert A in {row["n"] for row in merge_owner.admissions(case.store).values()}

    # The pending mixed train keeps its reservation; no extra solo train appears.
    support.gate(case)
    assert len(groups(case)) == 3 and composed_numbers(case) == [B, C, D]


def test_spent_single_half_mixed_train_lands_together_when_green(batched):
    case = batched
    red_train_then_retry(case)
    case.tracker.set_pr_checks(groups(case)[-1].number, "fail")
    support.gate(case)
    support.gate(case)
    assert composed_numbers(case) == [B, C, D]
    tip = case.tracker.pr_head_sha(groups(case)[-1].number)
    base = case.history.tip
    green(case)
    case.tracker.set_pr_checks(support.pr_for(case, A).number, "pending")
    support.gate(case)
    assert case.target_pushes == [(tip, "refs/heads/main", base)], batch.retirements(case.ctx)
    assert {n for n, _ in case.store.owner_merge_facts()} == {B, C, D}
    assert case.tracker.get_issue(A).state == "open"


def on_main(case, n):
    """Whether member n's PR head is reachable from origin main."""
    head = case.tracker.pr_head_sha(support.pr_for(case, n).number)
    return subprocess.run(
        ["git", "-C", str(case.history.origin), "merge-base", "--is-ancestor", head, "refs/heads/main"],
        capture_output=True).returncode == 0


def test_final_check_culprit_lets_verified_single_remainder_land_without_new_batch_ci(batched, monkeypatch):
    """PM r1039: batch [A, B] is green, the owner's final check names A, and B
    (PR CI green on the unchanged main) lands at once; C queued meanwhile does
    not pull B into a new train that waits for batch CI."""
    case = batched
    queue_workers(case, [A, B])
    orch_queue.set_global(case.store, [A, B])
    base = case.history.tip
    lock, armed = StateStore.merge_lock, [True]

    @contextmanager
    def culprit_at_final_check(store):
        with lock(store):
            if armed[0]:
                armed[0] = False
                case.tracker.set_pr_checks(support.pr_for(case, A).number, "fail")
            yield

    monkeypatch.setattr(StateStore, "merge_lock", culprit_at_final_check)
    support.gate(case)
    assert not armed[0], "the batch reached the owner's final check"
    retired, = batch.retirements(case.ctx)
    assert [m["n"] for m in retired["members"]] == [A, B] and retired["passed"]
    assert [m["n"] for m in retired["spent"]] == [A] and f"member #{A}" in retired["reason"]
    assert not case.target_pushes and not case.merges and case.history.tip == base
    assert case.tracker.pr_checks(support.pr_for(case, B).number) == "pass"

    queue_workers(case, [C])
    orch_queue.set_global(case.store, [A, B, C])
    case.pending_batch = True  # Any new batch CI stays pending for the rest of the test.
    trains_before = len(groups(case))
    for _ in range(2):
        support.gate(case)
        if on_main(case, B):
            break
    new_trains = [composed_numbers(case, pr) for pr in groups(case)[trains_before:]]
    assert on_main(case, B), (
        f"#{B} was verified on the current main and must land without a new batch CI; "
        f"new trains instead: {new_trains}, pushes {case.target_pushes}, merges {case.merges}"
    )
    assert git_command(case.history.origin, "show", f"main:src/member_{B}.py") == f"VALUE = {B}"
    assert not on_main(case, A), "the named culprit must not land"
    assert case.tracker.get_issue(A).state == "open"
    assert B in {n for n, _ in case.store.owner_merge_facts()}
    assert A not in {n for n, _ in case.store.owner_merge_facts()}

    # B lands once: further ticks do not land it again or put it in a train.
    landed_pushes, landed_merges = list(case.target_pushes), list(case.merges)
    support.gate(case)
    assert case.target_pushes[:len(landed_pushes)] == landed_pushes
    assert [m for m in case.merges if m in landed_merges] == landed_merges
    assert not [pr for pr in groups(case)[trains_before:] if B in composed_numbers(case, pr)], (
        f"#{B} was put into a new train after its verified landing"
    )
    assert sum(n == B for n, _ in case.store.owner_merge_facts()) == 1
