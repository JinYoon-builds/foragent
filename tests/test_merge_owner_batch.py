"""Merge-owner batches on real Git and measured tip CI: red trains retry an ordered half, then the remainder; failed sets resolve.

The offline transport uses fetch/update-ref, never forbidden Git commands.
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import merge_owner, merge_owner_batch as batch, orch_queue
from foragent.scheduler import TickReport
from helpers.merge_batch import (  # noqa: F401
    batch_candidates, batched, composed_numbers, edit_member_shared_file, edit_member_workflows, events, fail_tip, green, groups,
    queue_workers, refs, retry_case, update_base,
)


def test_red_train_retries_ordered_half_then_whole_remainder_and_halves_again(retry_case):
    case = retry_case
    numbers = [5, 1, 4, 2, 6, 3]
    queue_workers(case, numbers)
    orch_queue.set_global(case.store, numbers)
    support.gate(case)
    assert composed_numbers(case) == numbers
    fail_tip(case)
    assert not case.target_pushes and not case.merges
    queue_workers(case, [7])
    orch_queue.set_global(case.store, [7, *reversed(numbers)])
    support.gate(case)
    assert composed_numbers(case) == numbers[:3]
    assert len(groups(case)) == 2 and not case.target_pushes
    support.gate(case)  # Pending CI reserves the same half across owner restarts.
    assert len(groups(case)) == 2
    green(case)
    support.gate(case)
    assert len(case.target_pushes) == 1 and not case.merges
    assert {n for n, _ in case.store.owner_merge_facts()} == set(numbers[:3])
    case.pending_batch = True
    support.gate(case)
    assert composed_numbers(case) == numbers[3:]
    fail_tip(case)
    support.gate(case)
    assert composed_numbers(case) == numbers[3:5]
    assert len(case.target_pushes) == 1 and not case.merges
    assert [row["reason"] for row in batch.retirements(case.ctx)].count("tip CI red") == 2


def test_red_singleton_is_spent_and_returns_to_the_existing_single_path(batched):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    fail_tip(case)
    support.gate(case)
    assert composed_numbers(case) == [5]
    assert not case.target_pushes and not case.merges
    fail_tip(case)
    spent = [member for row in batch.retirements(case.ctx) for member in row["spent"]]
    assert [(member["n"], member["started_at"]) for member in spent] == [
        (5, case.store.get_run(5).started_at)]
    green(case)
    case.tracker.set_pr_checks(support.pr_for(case, 5).number, "fail")
    support.gate(case)
    support.gate(case)
    case.tracker.set_pr_checks(support.pr_for(case, 5).number, "fail")
    support.gate(case)
    assert (5, spent[0]["started_at"]) not in merge_owner.admissions(case.store)
    assert len(case.target_pushes) == 1 and not case.merges
    assert case.tracker.get_issue(5).state == "open"
    assert "tip CI red" in {row["reason"] for row in batch.retirements(case.ctx)}


def test_timed_out_retry_releases_unrelated_batch_on_next_tick(batched):
    case = batched
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    fail_tip(case)
    support.gate(case)
    assert composed_numbers(case) == [5]
    case.clock[0] += timedelta(seconds=batch.DEADLINE_SECONDS)
    support.gate(case)
    assert not refs(case) and "silence" in events(case)[-1]["reason"]

    queue_workers(case, [4, 2])
    support.gate(case)
    assert len(groups(case)) == 3 and composed_numbers(case) == [4, 2]
    assert case.history.tip == case.base and not case.target_pushes and not case.merges
    assert {p.n for p, _, _ in batch_candidates(case)} == {5, 1, 4, 2}
    assert all(not row["spent"] for row in batch.retirements(case.ctx))
    support.gate(case)
    assert len(groups(case)) == 3 and composed_numbers(case) == [4, 2]


@pytest.mark.parametrize("reason", ["target changed", "prefix PR changed"])
def test_non_green_retry_retirement_resolves_failed_set_across_bases(batched, reason):
    case = batched
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    fail_tip(case)
    support.gate(case)
    assert composed_numbers(case) == [5]
    if reason == "target changed":
        # Jin r1313: only a move overlapping a member's files retires the train.
        update_base(case, {"src/member_5.py": "VALUE = 'moved during retry'\n"})
    else:
        case.tracker.set_pr_head_sha(groups(case)[-1].number, case.base)
    support.gate(case)
    assert events(case)[-1]["reason"] == reason
    # A fresh identity on a newer target must not revive the failed-set restriction.
    update_base(case, {"src/neighbor.py": "VALUE = 'moved after retirement'\n"})
    queue_workers(case, [4, 2])
    orch_queue.set_global(case.store, [4, 2, 5, 1])
    support.gate(case)
    assert len(groups(case)) == 3 and composed_numbers(case) == [4, 2]
    assert {p.n for p, _, _ in batch_candidates(case)} == {5, 1, 4, 2}
    assert all(not row["spent"] for row in batch.retirements(case.ctx))
    assert not case.target_pushes and not case.merges
    support.gate(case)
    assert len(groups(case)) == 3 and composed_numbers(case) == [4, 2]
    assert case.git.remote_branch_sha(groups(case)[-1].head)


def test_uncomposable_failed_half_falls_through_to_normal_batching(batched, monkeypatch):
    case = batched
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    case.pending_batch = True
    edit_member_shared_file(case, monkeypatch, {5})
    queue_workers(case, [5, 1])
    support.gate(case)
    fail_tip(case)
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    queue_workers(case, [4, 2])
    report = support.gate(case)
    assert len(groups(case)) == 2 and composed_numbers(case) == [1, 4], report.actions
    assert [action for action in report.actions if "#5" in action] == [
        f"#5 sent back to lane: conflicts with {case.history.tip[:8]}"]
    assert len(batch.retirements(case.ctx)) == 1
    assert {p.n for p, _, _ in batch_candidates(case)} == {1, 4, 2}
    assert support.retry_marker(case.store.root, 5).exists()
    assert not case.target_pushes and not case.merges


def test_retired_composed_half_falls_through_to_normal_batching(batched):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1, 4, 2])
    support.gate(case)
    fail_tip(case)
    support.gate(case)
    assert composed_numbers(case) == [5, 1]
    tip = groups(case)[-1]
    base, chain = batch._decode(case.git, tip.head, case.tracker.pr_head_sha(tip.number))
    # Green CI with no landing leaves the parent unresolved, but this identity is retired.
    batch._close(case.ctx, TickReport(), base, chain, "final check stopped", passed=True)
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    support.gate(case)
    assert len(groups(case)) == 3 and composed_numbers(case) == [4, 2]
    assert not case.target_pushes and not case.merges


def test_failed_half_uses_join_exclusions_without_filling_from_outsiders(retry_case, monkeypatch):
    case = retry_case
    edit_member_shared_file(case, monkeypatch, {1})
    queue_workers(case, [5, 1, 4, 2, 6, 3, 7])
    support.gate(case)
    assert composed_numbers(case) == [5, 1, 4, 2, 6, 3]
    fail_tip(case)
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    green(case)
    report = TickReport()
    proof = batch.prepare(case.ctx, report, batch_candidates(case))
    assert proof, report.actions
    assert [n for n, *_ in proof.chain] == [5, 4]
    assert [action for action in report.actions if "#1" in action] == [
        f"#1 sent back to lane: conflicts with {case.history.tip[:8]}"]
    excluded = case.tracker.pr_acceptance_reports(proof.chain[-1][2])[0]["report"]["excluded"]
    assert "tests/acceptance/test_own_1.py" in excluded
    assert all(f"tests/acceptance/test_own_{n}.py" not in excluded for n in [5, 4])


@pytest.mark.parametrize("resolution", ["landed", "left"])
def test_resolved_failed_set_does_not_constrain_normal_composition(retry_case, resolution):
    case = retry_case
    numbers = [5, 1, 4, 2]
    queue_workers(case, numbers)
    support.gate(case)
    fail_tip(case)
    if resolution == "landed":
        case.config = replace(case.config, merge_batch=False)
        case.ctx.config = case.config
        green(case)
        support.drain(case, count=4)
        assert len(case.merges) == 4
        case.config = replace(case.config, merge_batch=True)
        case.ctx.config = case.config
    else:
        for n in numbers:
            case.tracker.close_issue(n)
    case.pending_batch = True
    queue_workers(case, [6, 3])
    support.gate(case)
    assert composed_numbers(case) == [6, 3]
    assert all(member["n"] in numbers for row in batch.retirements(case.ctx)
               if row["reason"] == "tip CI red" for member in row["members"])


@pytest.mark.parametrize("later", ["same-base", "partial", "changed-head", "full"])
def test_only_a_later_green_full_set_on_moved_main_resolves_a_failure(retry_case, later):
    case = retry_case
    numbers = [5, 1, 4, 2]
    queue_workers(case, numbers)
    support.gate(case)
    fail_tip(case)
    failed, = batch.retirements(case.ctx)
    old_base = case.base
    update_base(case, {"src/neighbor.py": "VALUE = 'new target'\n"})
    members = [dict(member) for member in failed["members"]]
    if later == "partial":
        members = members[:2]
    elif later == "changed-head":
        members[0]["head"] = case.base
    # Known green CI remains useful even when a fresh final check stopped landing.
    batch._record_retirement(case.ctx, "later-green", case.base, base=old_base if later == "same-base" else case.base,
                             reason="member #1: single path final check waiting", members=members,
                             refs={}, spent=[], passed=True)
    queue_workers(case, [6])
    candidates = batch_candidates(case)
    candidates.sort(key=lambda candidate: candidate[0].n != 6)
    assert batch.prepare(case.ctx, TickReport(), candidates) is False
    assert composed_numbers(case) == ([6, *numbers] if later == "full" else numbers[:2])
    assert not case.target_pushes and not case.merges
    assert next(row for row in batch.retirements(case.ctx) if row["identity"] == failed["identity"])["reason"] == "tip CI red"


def test_newest_failure_wins_over_git_ref_sort_order(retry_case):
    case = retry_case
    queue_workers(case, [5, 1, 4, 2, 6, 3])
    support.gate(case)
    fail_tip(case)
    failed, = batch.retirements(case.ctx)
    batch._record_retirement(case.ctx, "000-newer-failure", failed["candidate"], base=case.base,
                             reason="tip CI red", members=failed["members"][3:], refs={}, spent=[])
    assert batch.prepare(case.ctx, TickReport(), batch_candidates(case)) is False
    assert composed_numbers(case) == [2, 6]
