"""Merge-owner batches on real Git and measured tip CI: restarts and retirements keep members unspent; main conflicts return to their lanes.

The offline transport uses fetch/update-ref, never forbidden Git commands.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json
import subprocess

import pytest

from conftest import git_command
from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import merge_owner, merge_owner_batch as batch, run_group_evidence
from foragent.config import ConfigError, load_config
from foragent.git import GitError
from foragent.scheduler import TickReport
from foragent.state import StateStore
from foragent.tracker import RejectedEvidence, TrackerError
from helpers.merge_batch import (  # noqa: F401
    batch_candidates, batched, composed_numbers, edit_member_shared_file, edit_member_workflows, events,
    green, groups, published_train, queue_workers, refs, update_base,
)


@pytest.mark.parametrize("lag", [False, True], ids=["github-current", "github-lag"])
def test_restart_after_atomic_push_records_every_member_from_ancestry(batched, lag):
    case = batched
    case.github_lag = lag
    queue_workers(case, [5, 1, 4])

    def crashed():
        raise KeyboardInterrupt("Owner stopped after target push, before recording")

    case.after_target_push = crashed
    with pytest.raises(KeyboardInterrupt):
        support.gate(case)
    assert len(case.target_pushes) == 1 and not case.store.owner_merge_facts()
    case.tracker.remove_labels(1, ["approved"])
    case.after_target_push = None
    support.gate(case)
    assert len(case.target_pushes) == 1 and not case.merges and not refs(case)
    assert len(case.store.owner_merge_facts()) == 3 and not merge_owner.admissions(case.store)
    assert events(case)[-1]["used_members"] == 3


def test_github_lag_after_successful_push_reconciles_without_duplicate_landing(batched):
    case = batched
    case.github_lag = True
    queue_workers(case, [5, 1])
    support.gate(case)
    assert len(case.target_pushes) == 1 and not refs(case)
    assert len(case.store.owner_merge_facts()) == 2 and not merge_owner.admissions(case.store)
    case.history.update({"src/neighbor.py": "VALUE = 'later commit'\n"})
    support.gate(case)
    assert len(case.target_pushes) == 1 and not case.merges
    case.github_lag = False
    support.gate(case)
    facts = case.store.owner_merge_facts()
    assert len(facts) == 2 and all(fact["merged_at"] for fact in facts.values())
    assert not merge_owner.admissions(case.store)


def test_completion_io_failure_replays_all_members_after_atomic_push(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    record, failed = run_group_evidence.record_merge, []

    def unavailable_once(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise OSError("completion journal unavailable")
        return record(*args, **kwargs)

    monkeypatch.setattr(run_group_evidence, "record_merge", unavailable_once)
    support.gate(case)
    support.gate(case)
    assert failed and len(case.target_pushes) == 1 and not case.merges
    assert len(case.store.owner_merge_facts()) == 2 and not refs(case)


def test_cleanup_accepts_tip_pr_already_marked_merged_by_github(batched):
    case = batched
    case.merge_tip_metadata = True
    queue_workers(case, [5, 1])
    support.gate(case)
    assert len(case.target_pushes) == 1 and not refs(case)
    assert case.tracker.get_pr(groups(case)[0].number).state == "closed"


def test_cleanup_restart_keeps_the_retired_tip_and_never_reuses_evidence(batched, monkeypatch, capsys):
    case = batched
    queue_workers(case, [5, 1])
    transport, failed = case.git._run, []

    def unavailable_once(argv):
        if argv[0] == "push" and argv[-1].startswith(":refs/heads/" + batch.PREFIX) and not failed:
            failed.append(True)
            raise GitError("cleanup unavailable")
        return transport(argv)

    monkeypatch.setattr(case.git, "_run", unavailable_once)
    support.gate(case)
    assert failed and refs(case) and not merge_owner.admissions(case.store)
    retirement, = batch.retirements(case.ctx)
    reason = retirement["reason"]
    assert len(events(case)) == 1 and events(case)[0]["reason"] == reason
    assert f"retired: {reason}" in capsys.readouterr().out
    support.gate(case)
    assert not refs(case) and len(events(case)) == 1 and len(case.target_pushes) == 1
    assert batch.retirements(case.ctx)[0]["reason"] == reason


@pytest.mark.parametrize("legacy", [False, True], ids=["new", "legacy"])
def test_retirement_reason_is_durable_logged_and_preserved_on_replay(batched, capsys, legacy):
    case = batched
    marker = f"{batch.SPENT}reason-test"
    members = [dict(n=5, head=case.base, started_at=case.store.get_run(5).started_at)]
    if legacy:
        ledger = json.dumps(dict(candidate=case.base, members=members))
        old = git_command(case.root, "-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                          "commit-tree", batch._tree(case.git, case.base), "-p", case.base,
                          "-m", f"Legacy retirement\n\nForagent-Batch: {ledger}")
        git_command(case.root, "update-ref", marker, old)
    reason = batch._record_retirement(case.ctx, "reason-test", case.base, reason="construction conflict",
                                      members=members, refs={}, spent=[])
    saved = case.git.ref_sha(marker)
    retirement, = batch.retirements(case.ctx)
    assert retirement["members"] == members and retirement["reason"] == reason
    if legacy:
        assert reason.startswith("unknown (")
        assert git_command(case.root, "show", "-s", "--format=%P", saved) == old
    else:
        assert reason == "construction conflict"
    assert f"retired: {reason}" in capsys.readouterr().out
    assert batch._record_retirement(case.ctx, "reason-test", case.base,
                                    reason="candidate already retired", members=[], refs={}, spent=[]) == reason
    assert case.git.ref_sha(marker) == saved
    assert batch.retirements(case.ctx) == [retirement]
    output = capsys.readouterr().out
    assert f"retired: {reason}" in output and "candidate already retired" not in output

def test_disabling_batching_retires_published_prefixes_idempotently(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    published_train(case, monkeypatch)
    case.config = replace(case.config, merge_batch=False)
    case.ctx.config = case.config
    monkeypatch.setattr(batch, "prepare", lambda *args: pytest.fail("Disabled batch prepared evidence"))
    support.drain(case, count=2)
    support.gate(case)
    assert not refs(case) and len(events(case)) == 1
    assert len(case.merges) == 2 and not case.target_pushes and events(case)[0]["used_members"] == 0


@pytest.mark.parametrize("reason", ["moved", "deadline", "missing-pr", "damaged"])
def test_external_retirement_does_not_spend_member_generations(batched, monkeypatch, reason):
    case = batched
    queue_workers(case, [5, 1])
    published_train(case, monkeypatch)
    tip = case.tracker.get_pr(groups(case)[0].number)
    if reason == "moved":
        case.history.update({"src/neighbor.py": "VALUE = 37\n"})
    elif reason == "deadline":
        # No completed CI evidence may renew the silence window in this case.
        case.tracker.set_pr_checks(tip.number, "pending")
        case.clock[0] += timedelta(seconds=batch.DEADLINE_SECONDS)
    elif reason == "missing-pr":
        case.tracker.close_pr(support.pr_for(case, 5).number, delete_branch=False)
    else:
        git_command(case.history.origin, "update-ref", f"refs/heads/{tip.head}", case.base)
        case.tracker.set_pr_head_sha(tip.number, case.base)
    support.gate(case)
    retired = batch.retirements(case.ctx)
    assert retired and all(not row.get("spent", []) for row in retired)
    assert not case.target_pushes


def test_single_path_final_failure_spends_only_the_failing_member(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1, 4])
    lock = StateStore.merge_lock

    @contextmanager
    def fail_at_final_check(store):
        with lock(store):
            case.tracker.set_pr_checks(support.pr_for(case, 1).number, "fail")
            yield

    monkeypatch.setattr(StateStore, "merge_lock", fail_at_final_check)
    support.gate(case)
    assert not case.target_pushes and not case.merges and not refs(case)
    assert [m["n"] for row in batch.retirements(case.ctx) for m in row["spent"]] == [1]
    assert not case.store.owner_merge_facts()


def test_target_workflow_change_retires_train_without_spending_members(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    lock = StateStore.merge_lock

    @contextmanager
    def target_changes_under_lock(store):
        with lock(store):
            case.history.update({".github/workflows/foragent-acceptance.yml":
                                 "name: new-main-workflow\non: [pull_request]\njobs: {}\n"})
            yield

    monkeypatch.setattr(StateStore, "merge_lock", target_changes_under_lock)
    support.gate(case)
    tip, = groups(case)
    base, _ = batch._decode(case.git, tip.head, case.tracker.pr_head_sha(tip.number))
    assert base != case.history.tip
    retirement, = batch.retirements(case.ctx)
    assert retirement["reason"] == "member head or target changed"
    assert retirement["spent"] == []
    assert not case.target_pushes and not case.merges and not refs(case)
    assert {n for n, _ in merge_owner.admissions(case.store)} == {5, 1}


def test_reservation_also_guards_direct_single_path_advancement(batched, monkeypatch):
    case = batched
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    queue_workers(case, [5, 1, 4])
    published_train(case, monkeypatch)
    outsider = support.pr_for(case, 4)
    key, row = next((k, r) for k, r in merge_owner.admissions(case.store).items() if k[0] == 4)
    place = merge_owner.queue.Seat(*key, row["target"])
    job = merge_owner._load(case.ctx, place, row)
    assert not merge_owner.may_write_target(case.ctx, "main")
    assert merge_owner.may_write_target(case.ctx, "release")
    assert not merge_owner.advance_members(case.ctx, [(place, job, outsider)])
    assert not case.target_pushes and not case.merges


@pytest.mark.parametrize("failure", ["deadline", "missing", "rejected", "outage"])
def test_unusable_candidate_stops_evidence_and_members_resume_single_path(batched, monkeypatch, failure):
    case = batched
    queue_workers(case, [5, 1])
    published_train(case, monkeypatch)
    if failure == "deadline":
        # Finished tip CI is progress; only unfinished CI can reach the silence window.
        case.tracker.set_pr_checks(groups(case)[-1].number, "pending")
        case.clock[0] += timedelta(seconds=batch.DEADLINE_SECONDS)
    else:
        reports = case.tracker.pr_acceptance_reports

        def broken(head):
            if case.ci.by_head[head].branch.startswith(batch.PREFIX):
                if failure == "missing":
                    return []
                raise RejectedEvidence("invalid CI") if failure == "rejected" else TrackerError("CI unavailable")
            return reports(head)

        monkeypatch.setattr(case.tracker, "pr_acceptance_reports", broken)
        if failure == "missing":
            support.gate(case)
            assert refs(case) and not events(case) and not case.target_pushes and not case.merges
            case.clock[0] += timedelta(seconds=batch.DEADLINE_SECONDS)
    support.drain(case, count=2)
    retired = batch.retirements(case.ctx)
    members = [[member["n"] for member in row["members"]] for row in retired]
    assert not refs(case) and all(not row["spent"] for row in retired)
    if failure == "deadline":
        # Only the silent tip was unusable; tip CI itself is healthy, so after #5
        # takes the single path the remainder lands once as a batch of one.
        assert members == [[5, 1], [1]] and retired[-1]["reason"] == "train merged"
        assert len(case.target_pushes) == len(case.merges) == 1
        assert [event["result"] for event in events(case)] == ["unused", "used"]
    else:
        # Batch evidence stays unusable: every batch retires and both land on the single path.
        assert not case.target_pushes and len(case.merges) == 2
        # The remainder and its refreshed replacement are separate singleton identities.
        assert len(groups(case)) == len(retired) == 3 and members == [[5, 1], [1], [1]]
        assert retired[1]["members"][0]["head"] != retired[2]["members"][0]["head"]
        assert events(case)[-1]["result"] == "unused"
    assert sorted(n for n, _ in case.store.owner_merge_facts()) == [1, 5]
    assert all(case.tracker.get_issue(n).state == "closed" for n in [5, 1])


@pytest.mark.parametrize("interruption", ["crash", "throttle"])
def test_restart_recreates_missing_prefix_prs_from_refs_without_republishing(batched, monkeypatch, interruption):
    case = batched
    queue_workers(case, [5, 1])
    create, failed = case.tracker.create_pr, []

    def interrupted(**kwargs):
        if kwargs["head"].startswith(batch.PREFIX) and not failed:
            failed.append(True)
            if interruption == "crash":
                raise KeyboardInterrupt("Owner stopped after push, before PR creation")
            raise TrackerError("HTTP 429 API rate limit exceeded")
        return create(**kwargs)

    monkeypatch.setattr(case.tracker, "create_pr", interrupted)
    if interruption == "crash":
        with pytest.raises(KeyboardInterrupt):
            support.gate(case)
    else:
        support.gate(case)
        assert case.store.github_wait_until(case.clock[0]) is not None
    pushes = list(case.batch_pushes)
    assert refs(case) and not groups(case)
    case.clock[0] += timedelta(minutes=2)
    support.gate(case)
    assert len(groups(case)) == 1 and case.batch_pushes == pushes
    assert len(case.target_pushes) == 1 and len(case.store.owner_merge_facts()) == 2 and not refs(case)


def test_main_conflict_returns_member_to_lane_and_fills_batch_from_later_candidates(batched, monkeypatch):
    case = batched
    case.config = replace(case.config, merge_batch_size=3)
    case.ctx.config = case.config
    edit_member_shared_file(case, monkeypatch, {1})
    queue_workers(case, [5, 1, 4, 2])
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    candidates = batch_candidates(case)
    report = TickReport()
    proof = batch.prepare(case.ctx, report, candidates)
    assert proof, report.actions
    assert [n for n, *_ in proof.chain] == [5, 4, 2]
    tip, = groups(case)
    _, chain = batch._decode(case.git, tip.head, case.tracker.pr_head_sha(tip.number))
    assert chain == proof.chain
    skipped = [action for action in report.actions if "#1" in action]
    assert skipped == [f"#1 sent back to lane: conflicts with {case.history.tip[:8]}"]
    excluded = case.tracker.pr_acceptance_reports(proof.chain[-1][2])[0]["report"]["excluded"]
    assert "tests/acceptance/test_own_1.py" in excluded
    assert all(f"tests/acceptance/test_own_{n}.py" not in excluded for n in [5, 4, 2])
    assert not batch.retirements(case.ctx)
    assert set(merge_owner.admissions(case.store)) == {(p.n, p.started_at) for p, _, _ in candidates if p.n != 1}
    assert case.tracker.get_pr(support.pr_for(case, 1).number).state == "closed"
    issue = case.tracker.get_issue(1)
    assert issue.state == "open" and "ready" in issue.labels and "blocked" not in issue.labels
    assert support.retry_marker(case.store.root, 1).exists()
    job = json.loads((case.store.root / "merge-owner/1.json").read_text())
    assert job["phase"] == "repair-requested"
    assert job["failure"] == f"Base refresh failed (base-changed); repair the PR on {case.history.tip}."


def test_conflict_with_earlier_member_skips_only_that_member(batched, monkeypatch):
    case = batched
    case.config = replace(case.config, merge_batch_size=3)
    case.ctx.config = case.config
    edit_member_shared_file(case, monkeypatch, {5, 1})
    queue_workers(case, [5, 1, 4, 2])
    candidates = batch_candidates(case)
    for _, job, _ in candidates:
        case.git._call(["merge-tree", "--write-tree", case.base, job["head"]])
    report = TickReport()
    proof = batch.prepare(case.ctx, report, candidates)
    assert proof, report.actions
    assert [n for n, *_ in proof.chain] == [5, 4, 2]
    assert len([action for action in report.actions if "#1" in action]) == 1
    assert not batch.retirements(case.ctx)
    assert set(merge_owner.admissions(case.store)) == {(p.n, p.started_at) for p, _, _ in candidates}
    assert case.tracker.get_pr(support.pr_for(case, 1).number).state == "open"
    assert not support.retry_marker(case.store.root, 1).exists()


@pytest.mark.parametrize("conflicting", [(1,), (1, 4)])
def test_all_main_conflicts_return_to_lanes_in_one_pass_and_only_once(batched, monkeypatch, conflicting):
    case = batched
    edit_member_shared_file(case, monkeypatch, set(conflicting))
    queue_workers(case, [5, *conflicting, 2])
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    base = case.history.tip
    close, closed = case.tracker.close_pr, []

    def closing(pr, *args, **kwargs):
        if pr in {support.pr_for(case, n).number for n in conflicting}:
            closed.append(pr)
        return close(pr, *args, **kwargs)

    monkeypatch.setattr(case.tracker, "close_pr", closing)
    report = support.gate(case)
    assert composed_numbers(case) == [5, 2], report.actions
    assert len(case.target_pushes) == 1 and not case.merges
    assert not merge_owner.admissions(case.store)
    for n in conflicting:
        assert report.actions.count(f"#{n} sent back to lane: conflicts with {base[:8]}") == 1
        assert case.tracker.get_pr(support.pr_for(case, n).number).state == "closed"
        assert "ready" in case.tracker.get_issue(n).labels
        assert support.retry_marker(case.store.root, n).exists()
    assert len(closed) == len(conflicting)
    again = support.gate(case)
    assert len(closed) == len(conflicting)
    assert not any("sent back to lane" in action for action in again.actions)


def test_single_main_conflict_returns_to_lane_without_a_batch(batched, monkeypatch):
    case = batched
    edit_member_shared_file(case, monkeypatch, {5})
    queue_workers(case, [5])
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    report = support.gate(case)
    assert report.actions.count(f"#5 sent back to lane: conflicts with {case.history.tip[:8]}") == 1
    assert not groups(case) and not case.target_pushes and not case.merges
    assert not merge_owner.admissions(case.store)
    assert case.tracker.get_pr(support.pr_for(case, 5).number).state == "closed"
    assert "ready" in case.tracker.get_issue(5).labels
    assert support.retry_marker(case.store.root, 5).exists()
    job = json.loads((case.store.root / "merge-owner/5.json").read_text())
    assert job["phase"] == "repair-requested"


def test_workflow_exclusion_does_not_send_a_main_conflict_to_lane(batched, monkeypatch):
    case = batched
    edit_member_shared_file(case, monkeypatch, {1})
    edit_member_workflows(case, monkeypatch, {1: ".github/workflows/foragent-acceptance.yml"})
    queue_workers(case, [5, 1, 4])
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    report = TickReport()
    proof = batch.prepare(case.ctx, report, batch_candidates(case))
    # #2893: the workflow member takes its own single turn in ready order. Its
    # refresh onto the moved main conflicts, so that turn sends it to its lane
    # for repair; it is never put into the train, and the train still forms.
    assert proof and [n for n, *_ in proof.chain] == [5, 4], report.actions
    assert not any(action.startswith("#1 merge train excluded") for action in report.actions)
    assert {n for n, _ in merge_owner.admissions(case.store)} == {5, 4}
    assert case.tracker.get_pr(support.pr_for(case, 1).number).state == "closed"
    assert support.retry_marker(case.store.root, 1).exists()


def test_unavailable_base_conflict_probe_keeps_admission_for_single_path(batched, monkeypatch):
    case = batched
    edit_member_shared_file(case, monkeypatch, {1})
    queue_workers(case, [5, 1, 4])
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    base, head = case.history.tip, case.tracker.pr_head_sha(support.pr_for(case, 1).number)
    transport, probes = case.git._run, []

    def unavailable(args):
        if args == ["merge-tree", "--write-tree", base, head]:
            probes.append(args)
            raise subprocess.CalledProcessError(128, ["git", *args], stderr="base probe unavailable")
        return transport(args)

    monkeypatch.setattr(case.git, "_run", unavailable)
    report = TickReport()
    proof = batch.prepare(case.ctx, report, batch_candidates(case))
    assert proof and [n for n, *_ in proof.chain] == [5, 4], report.actions
    assert len(probes) == 1
    assert {n for n, _ in merge_owner.admissions(case.store)} == {5, 1, 4}
    assert case.tracker.get_pr(support.pr_for(case, 1).number).state == "open"
    assert not support.retry_marker(case.store.root, 1).exists()
    assert not any("sent back to lane" in action for action in report.actions)


def test_main_conflict_rechecks_pr_head_before_returning_to_lane(batched, monkeypatch):
    case = batched
    edit_member_shared_file(case, monkeypatch, {1})
    queue_workers(case, [5, 1, 4])
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    candidates = batch_candidates(case)
    pr = support.pr_for(case, 1).number
    changed_head = case.tracker.pr_head_sha(support.pr_for(case, 4).number)
    assert not case.git.is_ancestor(changed_head, case.history.tip)
    case.tracker.set_pr_head_sha(pr, changed_head)
    report = TickReport()
    proof = batch.prepare(case.ctx, report, candidates)
    assert proof and [n for n, *_ in proof.chain] == [5, 4], report.actions
    assert case.tracker.get_pr(pr).state == "open"
    assert not support.retry_marker(case.store.root, 1).exists()
    job = json.loads((case.store.root / "merge-owner/1.json").read_text())
    assert job["phase"] == "held"
    assert not any("sent back to lane" in action for action in report.actions)


def test_main_conflict_repair_resumes_after_lost_close_response(batched, monkeypatch):
    case = batched
    edit_member_shared_file(case, monkeypatch, {1})
    queue_workers(case, [5, 1, 4])
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    pr = support.pr_for(case, 1).number
    close, closed = case.tracker.close_pr, []

    def lost_response(number, *args, **kwargs):
        close(number, *args, **kwargs)
        if number == pr:
            closed.append(number)
            raise TrackerError("Lost close response")

    monkeypatch.setattr(case.tracker, "close_pr", lost_response)
    support.gate(case)
    assert composed_numbers(case) == [5, 4]
    assert case.tracker.get_pr(pr).state == "closed"
    assert merge_owner.pending(case.store, 1)
    assert not support.retry_marker(case.store.root, 1).exists()
    job = json.loads((case.store.root / "merge-owner/1.json").read_text())
    assert job["phase"] == "repair"
    support.gate(case)
    assert closed == [pr]
    assert not merge_owner.pending(case.store, 1)
    assert "ready" in case.tracker.get_issue(1).labels
    assert support.retry_marker(case.store.root, 1).exists()


def test_main_conflict_repair_rate_limit_stops_batch_publication_until_resumed(batched, monkeypatch):
    case = batched
    edit_member_shared_file(case, monkeypatch, {1})
    queue_workers(case, [5, 1, 4])
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    pr = support.pr_for(case, 1).number
    close, failed = case.tracker.close_pr, []

    def throttled(number, *args, **kwargs):
        if number == pr and not failed:
            failed.append(number)
            raise TrackerError("HTTP 429 API rate limit exceeded")
        return close(number, *args, **kwargs)

    monkeypatch.setattr(case.tracker, "close_pr", throttled)
    report = support.gate(case)
    backoff = case.store.github_wait_until(case.clock[0])
    assert failed == [pr] and backoff is not None
    assert not groups(case) and not refs(case) and not case.batch_pushes
    assert not case.target_pushes and not case.merges
    assert merge_owner.pending(case.store, 1)
    assert not any("sent back to lane" in action for action in report.actions)
    support.gate(case)
    assert not case.batch_pushes and not case.target_pushes
    case.clock[0] = backoff + timedelta(seconds=1)
    support.gate(case)
    assert composed_numbers(case) == [5, 4]
    assert len(case.target_pushes) == 1 and not case.merges
    assert not merge_owner.admissions(case.store)
    assert case.tracker.get_pr(pr).state == "closed"
    assert "ready" in case.tracker.get_issue(1).labels
    assert support.retry_marker(case.store.root, 1).exists()


@pytest.mark.parametrize("failure", ["save", "withdraw"])
def test_main_conflict_partial_repair_does_not_report_return_to_lane(batched, monkeypatch, failure):
    case = batched
    edit_member_shared_file(case, monkeypatch, {1})
    queue_workers(case, [5, 1, 4])
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    failed = []
    if failure == "save":
        save = merge_owner._save

        def interrupted(ctx, place, job):
            if place.n == 1 and job["phase"] == "repair-requested" and not failed:
                failed.append(place.n)
                raise OSError("repair completion save unavailable")
            return save(ctx, place, job)

        monkeypatch.setattr(merge_owner, "_save", interrupted)
    else:
        withdraw = merge_owner.queue.withdraw

        def interrupted(ctx, place):
            if place.n == 1 and not failed:
                failed.append(place.n)
                raise OSError("admission withdrawal unavailable")
            return withdraw(ctx, place)

        monkeypatch.setattr(merge_owner.queue, "withdraw", interrupted)
    report = support.gate(case)
    assert failed == [1]
    assert composed_numbers(case) == [5, 4]
    assert merge_owner.pending(case.store, 1)
    assert not any("#1 sent back to lane" in action for action in report.actions)
    support.gate(case)
    assert not merge_owner.pending(case.store, 1)
    assert case.tracker.get_pr(support.pr_for(case, 1).number).state == "closed"
    assert "ready" in case.tracker.get_issue(1).labels
    assert support.retry_marker(case.store.root, 1).exists()


def test_all_but_one_conflicting_gives_no_batch_or_retirement_on_retry(batched, monkeypatch):
    case = batched
    edit_member_shared_file(case, monkeypatch, {5, 1, 4, 2})
    queue_workers(case, [5, 1, 4, 2])
    candidates = batch_candidates(case)
    previous = None
    for _ in range(3):
        report = TickReport()
        proof = batch.prepare(case.ctx, report, candidates)
        assert proof and [n for n, *_ in proof.chain] == [5], report.actions
        if previous is None:
            assert all(len([a for a in report.actions if f"#{n}" in a]) == 1 for n in [1, 4, 2])
        else:
            assert proof.chain == previous.chain and proof.pr == previous.pr
            assert not report.actions
        previous = proof
    assert len(groups(case)) == len(case.batch_pushes) == 1 and refs(case) and not events(case)
    assert not batch.retirements(case.ctx)
    assert len(merge_owner.admissions(case.store)) == 4


def test_member_composition_failure_leaves_single_path_available(batched, monkeypatch):
    case = batched
    case.config = replace(case.config, merge_batch_size=3)
    case.ctx.config = case.config
    queue_workers(case, [5, 1, 4, 2])
    failed_head = case.tracker.pr_head_sha(support.pr_for(case, 1).number)
    call = case.git._call

    def unavailable(args):
        if args[:2] == ["merge-tree", "--write-tree"] and args[-1] == failed_head:
            raise GitError("member tree unavailable")
        return call(args)

    monkeypatch.setattr(case.git, "_call", unavailable)
    report = support.gate(case)
    tip, = groups(case)
    _, chain = batch._decode(case.git, tip.head, case.tracker.pr_head_sha(tip.number))
    assert [n for n, *_ in chain] == [5, 4, 2]
    assert len([a for a in report.actions if "#1" in a and "member tree unavailable" in a]) == 1
    assert len(case.target_pushes) == 1 and not case.merges
    assert all(not row["spent"] for row in batch.retirements(case.ctx))
    assert [place[0] for place in merge_owner.admissions(case.store)] == [1]
    monkeypatch.setattr(case.git, "_call", call)
    support.drain(case, count=1)
    assert not case.merges and len(case.target_pushes) == 2
    assert composed_numbers(case) == [1] and len(case.store.owner_merge_facts()) == 4


def test_retired_identity_uses_only_composed_members(batched, monkeypatch):
    case = batched
    edit_member_shared_file(case, monkeypatch, {5, 1})
    queue_workers(case, [5, 1, 4, 2])
    case.pending_batch = True
    first = support.gate(case)
    tip, = groups(case)
    _, chain = batch._decode(case.git, tip.head, case.tracker.pr_head_sha(tip.number))
    assert [n for n, *_ in chain] == [5, 4, 2], first.actions
    case.tracker.set_pr_checks(tip.number, "fail")
    support.gate(case)
    retired, = batch.retirements(case.ctx)
    assert retired["identity"] == batch._identity(case.base, chain)
    assert [member["n"] for member in retired["members"]] == [5, 4, 2]
    assert retired["candidate"] == chain[-1][2] and retired["spent"] == []
    assert batch.prepare(case.ctx, TickReport(), batch_candidates(case)) is False
    assert len(groups(case)) == 2 and refs(case)
    assert composed_numbers(case) == [5, 4]
    assert len(merge_owner.admissions(case.store)) == 4


def test_conflict_discards_entire_candidate_without_splitting_or_retry(batched, monkeypatch):
    """A member that conflicts with main is left out whole: no part of it reaches the tip, tried once."""
    case = batched
    case.config = replace(case.config, merge_batch_size=3)
    case.ctx.config = case.config
    edit_member_shared_file(case, monkeypatch, {1})
    queue_workers(case, [5, 1, 4, 2])
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    report = TickReport()
    proof = batch.prepare(case.ctx, report, batch_candidates(case))
    assert proof, report.actions
    assert 1 not in [n for n, *_ in proof.chain]
    tip = proof.chain[-1][2]
    assert git_command(case.root, "show", f"{tip}:src/shared.py") == "VALUE = -1"
    tip_files = git_command(case.root, "ls-tree", "-r", "--name-only", tip).split()
    assert all(f"src/member_{n}.py" in tip_files for n in [5, 4, 2])
    assert not [path for path in tip_files if "member_1" in path]
    assert len([action for action in report.actions if "#1" in action]) == 1


def test_incomplete_train_retirements_have_distinct_member_identities(batched, monkeypatch):
    """A retired train without a left-out member is identified by its composed members, not the queue."""
    case = batched
    edit_member_shared_file(case, monkeypatch, {1})
    queue_workers(case, [5, 1, 4, 2])
    update_base(case, {"src/shared.py": "VALUE = -1\n"})
    candidates = {place.n: (place, job) for place, job, _ in batch_candidates(case)}
    case.pending_batch = True
    first = support.gate(case)
    tip, = groups(case)
    _, chain = batch._decode(case.git, tip.head, case.tracker.pr_head_sha(tip.number))
    assert [n for n, *_ in chain] == [5, 4, 2]
    case.tracker.set_pr_checks(tip.number, "fail")
    support.gate(case)
    retired, = batch.retirements(case.ctx)
    place, job = candidates[1]
    with_left_out = [*chain[:1], (1, job["head"], chain[0][2], place.started_at), *chain[1:]]
    assert retired["identity"] == batch._identity(case.base, chain)
    assert retired["identity"] != batch._identity(case.base, with_left_out)
    assert [member["n"] for member in retired["members"]] == [5, 4, 2]
    # The left-out member went back to its lane for the main conflict; the retirement does not spend it.
    assert [a for a in first.actions if a.startswith("#1 ")] == [f"#1 sent back to lane: conflicts with {case.base[:8]}"]
    assert 1 not in [member["n"] for member in retired["spent"]] and 1 not in [place[0] for place in merge_owner.admissions(case.store)]


def test_merge_batch_false_uses_the_unchanged_single_path(proto, monkeypatch):
    case = proto
    monkeypatch.setattr(batch, "prepare", lambda *args: pytest.fail("Disabled batch was consulted"))
    edit_member_workflows(case, monkeypatch, {5: ".github/workflows/foragent-acceptance.yml"})
    queue_workers(case, [5, 1])
    support.drain(case, count=2)
    assert len(case.merges) == 2
    assert not groups(case) and not events(case)

@pytest.mark.parametrize("settings", ["merge_batch: yes\n", "merge_owner: true\nchecks: local\nmerge_batch: true\n", "merge_batch: 1\n"])
def test_merge_batch_requires_owner_ci_and_a_boolean(tmp_path, settings):
    (tmp_path / ".foragent.yml").write_text(settings)
    with pytest.raises(ConfigError, match="merge_batch"):
        load_config(tmp_path)


@pytest.mark.parametrize("rename", [False, True], ids=["delete", "rename"])
def test_dependency_removal_waits_for_running_train(batched, monkeypatch, rename):
    from foragent import main_write_requests as writes

    case = batched
    helper = "helpers.py"
    content = "VALUE = 1\n"
    case.history.update({helper: content})
    case.git.fetch("origin", "main")
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def feature(**kwargs):
        result = implement(**kwargs)
        (kwargs["worktree"] / "feature.py").write_text("from helpers import VALUE\n")
        return result

    monkeypatch.setattr(model, "run", feature)
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    tip, = groups(case)
    head = case.tracker.pr_head_sha(tip.number)
    base, chain = batch._decode(case.git, tip.head, head)
    assert helper not in batch.member_paths(case.git, base, chain)
    changes = {helper: {"before": writes._content(content.encode()), "after": None}}
    if rename:
        changes["renamed_helpers.py"] = {"before": None, "after": writes._content(content.encode())}
    row = writes.submit(case.store, "main", "Remove dependency", "test", {
        "kind": "files", "source": str(case.root), "base": base, "files": changes})
    support.gate(case)
    saved, = writes.requests(case.store)
    assert saved["id"] == row["id"] and saved["status"] == "pending"
    assert case.history.tip == base and refs(case)
    assert case.tracker.pr_head_sha(tip.number) == head
    assert git_command(case.history.origin, "show", f"main:{helper}") == content.strip()
    assert not case.store.owner_merge_facts()

    green(case)
    support.gate(case)
    assert {n for n, _ in case.store.owner_merge_facts()} == {5, 1}
    assert git_command(case.history.origin, "show", "main:feature.py") == "from helpers import VALUE"
    assert git_command(case.history.origin, "show", f"main:{helper}") == content.strip()
    support.gate(case)
    assert writes.requests(case.store)[0]["status"] == "done"
    assert helper not in git_command(case.history.origin, "ls-tree", "-r", "--name-only", "main").splitlines()


@pytest.mark.parametrize("rename", [False, True], ids=["delete", "rename"])
def test_durable_dependency_removal_cannot_reuse_train_evidence(batched, rename):
    from foragent import main_write_requests as writes

    case = batched
    case.history.update({"helpers.py": "VALUE = 1\n"})
    base = case.history.tip
    changes = {"helpers.py": None}
    if rename:
        changes["renamed_helpers.py"] = "VALUE = 1\n"
    case.history.update(changes)
    case.git.fetch("origin", "main")
    row = writes.submit(case.store, "main", "Remove dependency", "test", {"kind": "files", "files": {}})
    row["applying"] = case.history.tip
    writes._save(case.store, row)
    with pytest.raises(ValueError, match="target changed"):
        batch._accept_target(case.ctx, base, [], case.history.tip)
