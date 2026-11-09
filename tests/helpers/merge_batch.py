"""Shared merge-owner batch fixtures: a batching gate on real Git with measured tip CI."""

from dataclasses import replace
from pathlib import Path

import pytest

from conftest import git_command
import test_merge_owner_proto as support
from foragent import merge_owner, merge_owner_batch as batch, state_claims
from foragent.git import GitError
from foragent.tracker import FakeTracker, TrackerError
from helpers import batch_start as start


@pytest.fixture
def batched(proto, monkeypatch, tmp_path):
    case = proto
    case.config = replace(case.config, merge_batch=True)
    case.ctx.config = case.config
    case.ctx.reads = merge_owner.Snapshot(case.tracker)
    case.pending_batch = case.github_lag = case.merge_tip_metadata = False
    case.batch_pushes, case.target_pushes, case.target_attempts = [], [], []
    case.target_failure = case.after_target_push = None
    for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(key, case.clock[0].isoformat())
    transport = case.git._run

    def git_transport(argv):
        root, args = case.root, list(argv)
        while args[:1] == ["-C"]:
            root, args = Path(args[1]), args[2:]
        if args[:1] == ["push"]:
            source, destination = args[-1].split(":", 1) if ":" in args[-1] else (args[-1], args[-1])
            lease = next((a.split("=", 1)[1] for a in args if a.startswith("--force-with-lease=")), None)
            if lease is not None:
                ref, expected = lease.split(":", 1)
                actual = git_command(case.history.origin, "for-each-ref", "--format=%(objectname)", ref)
                if actual != expected:
                    raise GitError("lease rejected")
            if destination in {"main", "refs/heads/main"}:
                assert lease is not None and ref in {"main", "refs/heads/main"}
                with state_claims.hold(case.store.root / "merge-owner-pass.intent") as escaped:
                    assert not escaped, "Target push escaped the reserved merge owner"
                case.target_attempts.append((source, destination, expected))
                if case.target_failure:
                    raise GitError(case.target_failure)
                head = git_command(root, "rev-parse", source)
                git_command(root, "merge-base", "--is-ancestor", expected, head)
                git_command(case.history.origin, "fetch", str(root), source)
                git_command(case.history.origin, "update-ref", "refs/heads/main", head, expected)
                case.history.tip = head
                case.target_pushes.append((head, destination, expected))
                if case.after_target_push:
                    case.after_target_push()
                return ""
            if source and batch.PREFIX in destination:
                case.batch_pushes.append((source, destination))
        return transport(argv)

    monkeypatch.setattr(case.git, "_run", git_transport)
    create, get = case.tracker.create_pr, case.tracker.get_pr

    def created(**kwargs):
        if kwargs["head"].startswith(batch.PREFIX) and any(
                pr.head == kwargs["head"] and pr.base == kwargs["base"] and case.tracker.get_issue(pr.number).state == "open"
                for pr in case.tracker.created_prs):
            raise TrackerError("A pull request already exists for this head/base")
        pr = create(**kwargs)
        if kwargs["head"].startswith(batch.PREFIX) and case.pending_batch:
            case.tracker.set_pr_checks(pr, "pending")
        return pr

    def github_metadata(pr):
        row = get(pr)
        eligible = row.head.startswith("foragent/issue-") or case.merge_tip_metadata and row.head.startswith(batch.PREFIX)
        if eligible and not case.github_lag and not row.merged_at and row.state == "open":
            if case.git.at(case.history.origin).is_ancestor(row.head_sha, f"refs/heads/{row.base}"):
                FakeTracker.merge_pr(case.tracker, pr, expected_head_sha=row.head_sha, delete_branch=False)
                row = get(pr)
        return row

    monkeypatch.setattr(case.tracker, "create_pr", created)
    monkeypatch.setattr(case.tracker, "get_pr", github_metadata)
    reports = case.tracker.pr_acceptance_reports

    def measured(head):
        row = case.ci.by_head[head]
        return [] if row.branch.startswith(batch.PREFIX) and case.pending_batch else reports(head)

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", measured)
    start.prepare(case, tmp_path, monkeypatch)
    return case


def queue_workers(case, numbers):
    start.queued_once(case, numbers, support.queue_workers)
    if getattr(case, "pending_batch", False):
        for n in numbers:
            case.tracker.set_pr_checks(support.pr_for(case, n).number, "pending")


def batch_candidates(case):
    candidates = []
    for row in merge_owner.admissions(case.store).values():
        place = merge_owner.queue.Seat(row["n"], row["started_at"], row["target"])
        job = merge_owner._load(case.ctx, place, row)
        candidates.append((place, job, case.tracker.get_pr(job["pr"])))
    return candidates


def edit_member_shared_file(case, monkeypatch, numbers):
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def edit(**kwargs):
        result = implement(**kwargs)
        if case.worker.number in numbers:
            (kwargs["worktree"] / "src/shared.py").write_text(f"VALUE = {case.worker.number}\n")
        return result

    monkeypatch.setattr(model, "run", edit)


def edit_member_workflows(case, monkeypatch, paths):
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def edit(**kwargs):
        result = implement(**kwargs)
        if path := paths.get(case.worker.number):
            workflow = kwargs["worktree"] / path
            workflow.parent.mkdir(parents=True, exist_ok=True)
            workflow.write_text("name: changed-acceptance\non: [pull_request]\njobs: {}\n")
        return result

    monkeypatch.setattr(model, "run", edit)


def groups(case):
    return [pr for pr in case.tracker.created_prs if pr.head.startswith(batch.PREFIX)]


def refs(case):
    return git_command(case.history.origin, "for-each-ref", "--format=%(refname)", f"refs/heads/{batch.PREFIX}*")


def events(case):
    return [row["data"] for row in case.store.flow_records() if row["kind"] == "merge-batch"]


def green(case):
    case.pending_batch = False
    for pr in case.tracker.created_prs:
        case.tracker.set_pr_checks(pr.number, "pass")


def published_train(case, monkeypatch):
    """Interrupt after publication to exercise durable remote-ledger recovery."""
    checks = case.tracker.pr_checks

    def interrupted(pr):
        if case.tracker.get_pr(pr).head.startswith(batch.PREFIX):
            raise KeyboardInterrupt("Owner stopped after tip publication")
        return checks(pr)

    with monkeypatch.context() as patch:
        patch.setattr(case.tracker, "pr_checks", interrupted)
        with pytest.raises(KeyboardInterrupt):
            support.gate(case)
    assert len(groups(case)) == 1 and refs(case)
    assert not case.target_pushes and not events(case)


@pytest.fixture
def retry_case(request, monkeypatch):
    from helpers import merge_world as contract

    monkeypatch.setattr(contract, "NUMBERS", (5, 1, 4, 2, 6, 3, 7))
    case = request.getfixturevalue("batched")
    case.config = replace(case.config, merge_batch_size=6)
    case.ctx.config = case.config
    case.pending_batch = True
    return case


def composed_numbers(case, pr=None):
    pr = pr or groups(case)[-1]
    _, chain = batch._decode(case.git, pr.head, case.tracker.pr_head_sha(pr.number))
    return [n for n, *_ in chain]


def fail_tip(case):
    case.tracker.set_pr_checks(groups(case)[-1].number, "fail")
    support.gate(case)
    assert not refs(case)
    assert events(case)[-1]["reason"] == "tip CI red"


def update_base(case, files):
    case.history.update(files)
    git_command(case.root, "fetch", "origin")
    git_command(case.root, "merge", "--ff-only", "origin/main")
    case.base = case.history.tip
