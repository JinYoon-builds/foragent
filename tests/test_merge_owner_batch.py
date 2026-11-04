"""Real Git and measured tip CI; target publication is one compare-and-swap.

The offline transport uses fetch/update-ref, never forbidden Git commands.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import fcntl
import json
from pathlib import Path

import pytest

from acceptance.test_merge_path_real_settings import world  # noqa: F401
from conftest import git_command
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import ci_local, ci_mode, merge_owner, merge_owner_batch as batch, orch_queue, run_group_evidence
from foragent.config import ConfigError, load_config
from foragent.git import GitError
from foragent.issue import body_hash
from foragent.scheduler import TickReport
from foragent.state import StateStore
from foragent.tracker import FakeTracker, GhTracker, RejectedEvidence, TrackerError


@pytest.fixture
def batched(proto, monkeypatch):
    case = proto
    case.config = replace(case.config, merge_batch=True)
    case.ctx.config = case.config
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
                owner_lock = case.store.lock_acceptance_baseline("merge-owner", blocking=False)
                if owner_lock is not None:
                    owner_lock.close()
                assert owner_lock is None, "Target push escaped the independent owner lock"
                with (case.git.common_dir() / "foragent-publish.lock").open("a") as main_lock:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(main_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                case.target_attempts.append((source, destination, expected))
                if case.target_failure:
                    raise GitError(case.target_failure)
                head = git_command(root, "rev-parse", source)
                git_command(case.history.origin, "merge-base", "--is-ancestor", expected, head)
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
    return case


def queue_workers(case, numbers):
    support.queue_workers(case, numbers)
    if getattr(case, "pending_batch", False):
        for n in numbers:
            case.tracker.set_pr_checks(support.pr_for(case, n).number, "pending")


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



@pytest.mark.parametrize("numbers", [[5, 1], [2, 4, 1, 5], [5, 1, 4, 2, 6, 3]])
def test_only_tip_published_and_reused_by_single_path_across_restart(request, monkeypatch, numbers):
    if len(numbers) > 4:
        from acceptance import test_merge_path_real_settings as contract
        monkeypatch.setattr(contract, "NUMBERS", tuple(numbers))
    case = request.getfixturevalue("batched")
    case.config = replace(case.config, merge_batch_size=6)
    case.ctx.config = case.config
    queue_workers(case, numbers)
    orch_queue.set_global(case.store, numbers)
    base = case.history.tip
    heads = {n: case.tracker.pr_head_sha(support.pr_for(case, n).number) for n in numbers}
    report = support.gate(case)
    assert len(groups(case)) == len(case.batch_pushes) == len(case.target_pushes) == 1, report.actions
    pr = groups(case)[0]
    tip = case.tracker.pr_head_sha(pr.number)
    decoded_base, chain = batch._decode(case.git, pr.head, tip)
    assert decoded_base == base and pr.head == f"{batch.PREFIX}{base[:8]}-{len(numbers)}"
    assert [(n, head) for n, head, _, _ in chain] == [(n, heads[n]) for n in numbers]
    assert git_command(case.root, "show", "-s", "--format=%cn <%ce>", tip) == "foragent <foragent@localhost>"
    assert case.target_pushes == [(tip, "refs/heads/main", base)]
    assert case.history.tip == tip and not case.merges and not refs(case)
    assert all(case.git.is_ancestor(head, tip) for head in heads.values())
    assert len(case.store.owner_merge_facts()) == len(numbers)
    assert all(case.tracker.get_issue(n).state == "closed" for n in numbers)
    assert events(case)[-1]["result"] == "used" and events(case)[-1]["used_members"] == len(numbers)
    assert events(case)[-1]["ci_minutes"] == pytest.approx(2 / 60)
    support.gate(case)
    assert len(case.target_pushes) == 1 and len(case.store.owner_merge_facts()) == len(numbers)


def test_green_tip_lands_members_whose_own_ci_is_pending(batched):
    case = batched
    queue_workers(case, [5, 1])
    for n in [5, 1]:
        case.tracker.set_pr_checks(support.pr_for(case, n).number, "pending")
    support.gate(case)
    assert len(case.target_pushes) == 1 and not case.merges
    assert len(case.store.owner_merge_facts()) == 2


@pytest.mark.parametrize("path", [".github/workflows/foragent-acceptance.yml",
                                 ".github/workflows/foragent-acceptance-extra.yaml"])
def test_workflow_changing_member_uses_single_fallback_while_remaining_members_batch(batched, monkeypatch, path):
    case = batched
    edit_member_workflows(case, monkeypatch, {5: path})
    queue_workers(case, [5, 1, 4])
    changed_head = case.tracker.pr_head_sha(support.pr_for(case, 5).number)
    fallback, measured = ci_local.fallback_comparison, []

    def local(attempt, base_sha):
        measured.append(attempt.n)
        return fallback(attempt, base_sha)

    monkeypatch.setattr(ci_local, "fallback_comparison", local)
    support.gate(case)
    tip, = groups(case)
    _, chain = batch._decode(case.git, tip.head, case.tracker.pr_head_sha(tip.number))
    assert [n for n, *_ in chain] == [1, 4]
    assert all(head != changed_head for _, head, *_ in chain)
    assert len(case.target_pushes) == 1 and not case.merges
    support.drain(case, count=3)
    assert measured and set(measured) == {5}
    assert [row["pr"] for row in case.merges] == [support.pr_for(case, 5).number]
    assert len(groups(case)) == 1 and len(case.store.owner_merge_facts()) == 3
    assert all(case.tracker.get_issue(n).state == "closed" for n in [5, 1, 4])


def test_nonacceptance_workflow_change_remains_a_batch_member(batched, monkeypatch):
    case = batched
    edit_member_workflows(case, monkeypatch, {5: ".github/workflows/ci.yml"})
    queue_workers(case, [5, 1])
    support.gate(case)
    tip, = groups(case)
    _, chain = batch._decode(case.git, tip.head, case.tracker.pr_head_sha(tip.number))
    assert [n for n, *_ in chain] == [5, 1]
    assert len(case.target_pushes) == 1 and not case.merges


@pytest.mark.parametrize('path', ['.github/workflows/foragent-acceptance.yml',
                                 '.github/workflows/foragent-acceptance-extra.yaml'])
def test_main_only_workflow_change_keeps_old_members_in_batch(batched, monkeypatch, path):
    case = batched
    queue_workers(case, [5, 1])
    workflow = 'name: updated-main-workflow\non: [pull_request]\njobs: {}\n'
    case.history.update({path: workflow})
    base = case.history.tip

    def unexpected(*args):
        pytest.fail('The batch tip uses the current main workflow and must use its CI evidence')

    monkeypatch.setattr(ci_local, 'fallback_comparison', unexpected)
    report = support.gate(case)
    pr, = groups(case)
    tip = case.tracker.pr_head_sha(pr.number)
    decoded_base, chain = batch._decode(case.git, pr.head, tip)
    assert decoded_base == base and [n for n, *_ in chain] == [5, 1]
    assert git_command(case.history.origin, 'show', f'{tip}:{path}') == workflow.strip()
    assert len(case.target_pushes) == 1 and not case.merges, report.actions
    assert len(case.store.owner_merge_facts()) == 2


def test_workflow_comparison_failure_falls_back_to_single_path(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    compare, failed = ci_mode.pr_changes_workflows, []

    def unavailable_once(*args):
        if not failed:
            failed.append(True)
            raise GitError("workflow diff unavailable")
        return compare(*args)

    monkeypatch.setattr(ci_mode, "pr_changes_workflows", unavailable_once)
    report = support.gate(case)
    assert failed and not groups(case) and not case.target_pushes
    assert len(case.merges) == 1
    assert any("workflow diff unavailable" in action for action in report.actions)


def test_remote_only_candidate_does_not_block_locally_available_batch(batched, monkeypatch, tmp_path):
    case = batched
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    queue_workers(case, [5, 1, 4])
    candidates = []
    for row in merge_owner.admissions(case.store).values():
        place = merge_owner.queue.Seat(row["n"], row["started_at"], row["target"])
        job = merge_owner._load(case.ctx, place, row)
        candidates.append((place, job, case.tracker.get_pr(job["pr"])))
        case.tracker.set_pr_checks(job["pr"], "pending")
    fresh = tmp_path / "fresh-owner"
    fresh.mkdir()
    git_command(fresh, "init", "-q")
    git_command(fresh, "remote", "add", "origin", str(case.history.origin))
    git = case.git.at(fresh)
    git.fetch("origin", "main")
    for _, job, _ in candidates[:2]:
        git._call(["fetch", "origin", job["head"]])
    missing_head = candidates[2][1]["head"]
    with pytest.raises(GitError):
        git._call(["cat-file", "-e", f"{missing_head}^{{commit}}"])
    monkeypatch.setattr(case.ctx, "git", git)
    report = TickReport()
    for _ in range(3):
        proof = batch.prepare(case.ctx, report, candidates)
        assert proof, report.actions
        assert [n for n, *_ in proof.chain] == [5, 1]
    assert len(groups(case)) == 1
    assert any("#4" in action and missing_head in action for action in report.actions)
    assert not batch.retirements(case.ctx)


def test_pending_tip_does_not_publish_other_prefixes(batched):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1, 4, 2])
    support.gate(case)
    assert len(groups(case)) == len(case.batch_pushes) == 1
    assert refs(case) and not case.target_pushes and not case.merges
    assert not events(case) and not batch.retirements(case.ctx)
    support.gate(case)
    assert len(case.batch_pushes) == 1
    green(case)
    support.drain(case)
    assert len(case.store.owner_merge_facts()) == 4
    assert len(case.batch_pushes) == len(case.target_pushes) == 1


@pytest.mark.parametrize("configured,expected", [(None, 4), (6, 6)], ids=["default", "configured-six"])
def test_batch_size_limits_prefixes_and_survives_restart(request, monkeypatch, configured, expected):
    from acceptance import test_merge_path_real_settings as contract
    numbers = (5, 1, 4, 2, 6, 3, 7)
    monkeypatch.setattr(contract, "NUMBERS", numbers)
    case = request.getfixturevalue("batched")
    settings = case.root / ".foragent.yml"
    if configured is not None:
        settings.write_text(settings.read_text() + f"merge_batch_size: {configured}\n")
    case.config = replace(case.config, merge_batch_size=load_config(case.root).merge_batch_size)
    case.ctx.config = case.config
    queue_workers(case, numbers)
    published_train(case, monkeypatch)
    base, chain = batch._decode(case.git, groups(case)[0].head, case.tracker.pr_head_sha(groups(case)[0].number))
    assert base == case.base and [n for n, *_ in chain] == list(numbers[:expected])
    support.gate(case)
    assert len(case.target_pushes) == 1 and len(case.store.owner_merge_facts()) == expected


@pytest.mark.parametrize("change", ["approval", "blocked", "needs-human", "generation", "approval-generation", "closed", "ci", "head", "prefix-ci", "prefix-reports", "policy"])
def test_ci_green_cannot_bypass_any_fresh_locked_single_path_check(batched, monkeypatch, change):
    case = batched
    queue_workers(case, [5, 1, 4])
    base, lock, changed = case.history.tip, StateStore.merge_lock, []

    @contextmanager
    def changed_under_lock(store):
        with lock(store):
            if not changed:
                changed.append(change)
                if change == "approval":
                    case.tracker.remove_labels(1, ["approved"])
                elif change in {"blocked", "needs-human"}:
                    case.tracker.add_labels(1, [change])
                elif change == "generation":
                    case.store.start_run(1, branch="replacement", worktree=case.store.root / "replacement")
                elif change == "approval-generation":
                    approval = case.store.load_approval(1)
                    case.store.save_approval(**replace(approval, approved_at=case.clock[0].isoformat()).__dict__)
                elif change == "closed":
                    case.tracker.close_issue(1)
                elif change == "ci":
                    case.tracker.set_pr_checks(support.pr_for(case, 1).number, "fail")
                elif change == "prefix-ci":
                    case.tracker.set_pr_checks(groups(case)[0].number, "fail")
                elif change == "prefix-reports":
                    case.pending_batch = True
                elif change == "policy":
                    case.tracker.add_labels(1, ["merge-hold"])
                else:
                    support.update_pr_branch(case, 1)
            yield

    if change == "policy":
        allows = merge_owner.run_publish._policy_allows
        monkeypatch.setattr(merge_owner.run_publish, "_policy_allows", lambda *a, **kw: False if changed else allows(*a, **kw))
    monkeypatch.setattr(StateStore, "merge_lock", changed_under_lock)
    support.gate(case)
    assert changed and case.history.tip == base
    if change == "prefix-reports":
        assert refs(case) and not case.target_pushes and not case.merges
        assert not case.store.owner_merge_facts() and not events(case)
        green(case)
        support.gate(case)
        assert len(case.target_pushes) == 1 and len(case.store.owner_merge_facts()) == 3
        return
    assert not case.target_pushes and not case.merges and not refs(case)
    assert not case.store.owner_merge_facts() and events(case)[-1]["result"] == "unused"


@pytest.mark.parametrize("red", ["checks", "regression"])
def test_red_tip_retires_whole_train_without_landing(batched, monkeypatch, red):
    case = batched
    queue_workers(case, [5, 1, 4])
    checks, reports = case.tracker.pr_checks, case.tracker.pr_acceptance_reports

    def status(pr):
        return "fail" if case.tracker.get_pr(pr).head.startswith(batch.PREFIX) else checks(pr)

    def regression(head):
        values = reports(head)
        if case.ci.by_head[head].branch.startswith(batch.PREFIX):
            for value in values:
                value["report"]["head"]["passing_ids"] = []
        return values

    monkeypatch.setattr(case.tracker, "pr_checks" if red == "checks" else "pr_acceptance_reports", status if red == "checks" else regression)
    base = case.history.tip
    support.gate(case)
    assert case.history.tip == base and not case.target_pushes and not case.merges and not refs(case)
    assert len(groups(case)) == 1 and events(case)[-1]["used_members"] == 0
    assert "red" in events(case)[-1]["reason"]


@pytest.mark.parametrize("failure", ["lease lost", "branch protection rejected"])
def test_rejected_target_push_retires_train_and_members_resume_single_path(batched, failure):
    case = batched
    queue_workers(case, [5, 1])
    base = case.history.tip
    case.target_failure = failure
    support.gate(case)
    assert len(case.target_attempts) == 1 and not case.target_pushes
    assert case.history.tip == base and not case.merges and not refs(case)
    assert failure in events(case)[-1]["reason"] and not batch.retirements(case.ctx)[0]["spent"]
    case.target_failure = None
    support.drain(case, count=2)
    assert len(case.merges) == 2 and not case.target_pushes


def test_target_moving_under_lock_is_rejected_even_when_its_tree_is_identical(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    lock = StateStore.merge_lock

    @contextmanager
    def moved(store):
        with lock(store):
            case.history.update({})
            yield

    monkeypatch.setattr(StateStore, "merge_lock", moved)
    support.gate(case)
    assert not case.target_pushes and not case.merges and not refs(case)
    assert events(case)[-1]["used_members"] == 0
    assert all(not row["spent"] for row in batch.retirements(case.ctx))


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
    assert not refs(case) and len(groups(case)) == 1 and not case.target_pushes
    assert events(case)[-1]["result"] == "unused"
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


def test_conflict_discards_entire_candidate_without_splitting_or_retry(batched, monkeypatch):
    case = batched
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def incompatible(**kwargs):
        result = implement(**kwargs)
        if case.worker.number in {5, 4}:
            (kwargs["worktree"] / "src/shared.py").write_text(f"VALUE = {case.worker.number}\n")
        return result

    monkeypatch.setattr(model, "run", incompatible)
    queue_workers(case, [5, 1, 4, 2])
    support.gate(case)
    assert not groups(case) and not refs(case)
    assert events(case)[-1]["result"] == "unused"
    assert [m["n"] for m in events(case)[-1]["members"]] == [5, 1, 4, 2]
    support.drain(case)
    assert not groups(case)  # Even the healthy suffix never forms a retry batch.

def test_incomplete_train_retirements_have_distinct_member_identities(batched, monkeypatch):
    case = batched
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def incompatible(**kwargs):
        result = implement(**kwargs)
        (kwargs["worktree"] / "src/shared.py").write_text(f"VALUE = {case.worker.number}\n")
        return result

    monkeypatch.setattr(model, "run", incompatible)
    queue_workers(case, [5, 1, 4, 2])
    for n in [5, 1, 4, 2]:
        case.tracker.set_pr_checks(support.pr_for(case, n).number, "pending")
    for _ in range(3):
        support.gate(case)
    retired = batch.retirements(case.ctx)
    assert len(retired) == 2
    assert {tuple(m["n"] for m in row["members"]) for row in retired} == {(5, 1), (4, 2)}
    assert all(row["candidate"] == case.base and not row["spent"] for row in retired)
    assert not groups(case) and len(events(case)) == 2

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


def test_retirement_cancels_owned_prefix_ci_before_deleting_refs_and_retries(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    published_train(case, monkeypatch)
    runs, owned = [], []
    for pr in groups(case):
        head = case.tracker.pr_head_sha(pr.number)
        for status in ["queued", "in_progress", "completed"]:
            row = dict(id=len(runs) + 1, head_sha=head, head_branch=pr.head, status=status,
                       repository={"full_name": case.ctx.repo}, head_repository={"full_name": case.ctx.repo})
            runs.append(row)
            if status != "completed":
                owned.append(row["id"])
        for fields in [dict(head_branch="other"), dict(head_sha="other"),
                       dict(head_repository={"full_name": "fork/repo"}),
                       dict(repository={"full_name": "other/repo"})]:
            runs.append({**row, "id": len(runs) + 1, "status": "queued", **fields})
    cancelled, failed = [], []

    def actions_api(args, input=None):
        if args[:4] == ["api", "--paginate", "--jq", ".workflow_runs"]:
            assert "head_sha=" in args[-1] and "per_page=100" in args[-1]
            return json.dumps(runs[:3]) + "\n" + json.dumps(runs[3:])
        assert args[:3] == ["api", "-X", "POST"] and args[-1].endswith("/cancel")
        row = next(row for row in runs if str(row["id"]) == args[-1].split("/")[-2])
        assert row["id"] in owned
        assert case.git.remote_branch_sha(row["head_branch"]) == row["head_sha"]
        if row["status"] == "in_progress" and not failed:
            failed.append(True)
            raise TrackerError("cancellation unavailable")
        row["status"] = "completed"
        cancelled.append(row["id"])
        return ""

    monkeypatch.setattr(case.tracker, "cancel_workflow_runs",
                        GhTracker(case.ctx.repo, run=actions_api).cancel_workflow_runs)
    transport = case.git._run

    def delete_after_cancel(argv):
        if argv[0] == "push" and argv[-1].startswith(":refs/heads/" + batch.PREFIX):
            branch = argv[-1].removeprefix(":refs/heads/")
            assert all(row["status"] == "completed" for row in runs
                       if row["id"] in owned and row["head_branch"] == branch)
        return transport(argv)

    monkeypatch.setattr(case.git, "_run", delete_after_cancel)
    case.clock[0] += timedelta(seconds=batch.DEADLINE_SECONDS)
    support.gate(case)
    assert failed and refs(case) and cancelled == owned[:1]
    support.gate(case)
    assert not refs(case) and cancelled == owned
    assert all(case.tracker.get_pr(pr.number).state == "closed" for pr in groups(case))
    assert len(events(case)) == 1


def test_retargeted_prefix_is_retained_by_the_owner_close_guard(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    published_train(case, monkeypatch)
    pr = case.tracker.get_pr(groups(case)[0].number)
    git_command(case.history.origin, "update-ref", "refs/heads/release", case.base)
    case.tracker.update_pr(pr.number, base="release", title=pr.title, body=pr.body)
    with pytest.raises(TrackerError, match="PR identity changed"):
        merge_owner.close_for_repair(case.ctx, None, pr.number, pr.head_sha, "main", branch=pr.head)
    report = support.gate(case)
    assert any("PR identity changed" in action for action in report.actions)
    assert case.tracker.get_pr(pr.number).state == "open"
    assert case.git.remote_branch_sha(pr.head) == pr.head_sha
    support.gate(case)
    assert case.tracker.get_pr(pr.number).state == "open"
    assert case.git.remote_branch_sha(pr.head) == pr.head_sha
    support.drain(case, count=2)
    assert len(case.store.owner_merge_facts()) == 2 and not case.target_pushes
    assert case.git.remote_branch_sha(pr.head) == pr.head_sha


def update_base(case, files):
    case.history.update(files)
    git_command(case.root, "fetch", "origin")
    git_command(case.root, "merge", "--ff-only", "origin/main")
    case.base = case.history.tip


def test_train_ci_includes_every_members_done_when_and_excludes_only_outsiders(batched):
    case = batched
    own = "tests/acceptance/test_own_5.py"
    update_base(case, {own: "from pathlib import Path\ndef test_own():\n"
                 "    assert Path('src/member_5.py').exists()\n"
                 "    assert not Path('src/member_1.py').exists()\n"})
    approval = case.store.load_approval(5)
    case.store.save_approval(**replace(approval, acceptance_blobs=case.git.blob_shas(case.base, [own])).__dict__)
    queue_workers(case, [5, 1])  # Both approved Done when commands pass individually.
    support.gate(case)
    tip = case.tracker.pr_head_sha(groups(case)[0].number)
    reports = case.tracker.pr_acceptance_reports(tip)
    excluded = reports[0]["report"]["excluded"]
    assert own not in excluded and "tests/acceptance/test_own_1.py" not in excluded
    assert "tests/acceptance/test_own_4.py" in excluded
    collected = {node for row in reports for node in row["report"]["head"]["collected_ids"]}
    passing = {node for row in reports for node in row["report"]["head"]["passing_ids"]}
    assert f"{own}::test_own" in collected - passing
    assert not case.target_pushes and not case.merges and case.history.tip == case.base
    assert not case.store.owner_merge_facts() and not refs(case)
    assert events(case)[-1]["result"] == "unused"


def test_train_validates_later_members_acceptance_locks_on_composed_tip(batched, monkeypatch):
    from foragent import runner
    from foragent.orch import merge

    case = batched
    own = "tests/acceptance/test_own_1.py"
    locked = ("from pathlib import Path\ndef test_own():\n"
              "    assert Path('src/member_1.py').exists()\n"
              "    assert not Path('src/member_5.py').exists()\n")
    update_base(case, {own: locked})
    approval = case.store.load_approval(1)
    case.store.save_approval(**replace(approval, acceptance_blobs=case.git.blob_shas(case.base, [own])).__dict__)
    approval = case.store.load_approval(5)
    body = approval.body.replace("- src/member_5.py", f"- src/member_5.py\n- {own}")
    case.tracker.update_issue(5, title=case.tracker.get_issue(5).title, body=body)
    case.store.save_approval(**replace(approval, body=body, body_sha=body_hash(body)).__dict__)
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def weaken_other_members_test(**kwargs):
        result = implement(**kwargs)
        if case.worker.number == 5:
            (kwargs["worktree"] / own).write_text(locked.replace(
                "    assert not Path('src/member_5.py').exists()\n", ""))
        return result

    monkeypatch.setattr(model, "run", weaken_other_members_test)
    queue_workers(case, [1])
    case.worker.number = 5
    case.clock[0] += timedelta(seconds=1)
    case.store.start_run(5, branch="foragent/issue-5", worktree=case.store.worktree_path(5))
    case.tracker.add_labels(5, ["running"])
    assert runner.run_issue(5, case.ctx) == "held"  # Acceptance edits require explicit authorization.
    case.clock[0] += timedelta(seconds=1)
    assert "merge-queued" in merge(case.ctx, support.pr_for(case, 5).number, approved_by="human")
    orch_queue.set_global(case.store, [5, 1])
    support.gate(case)
    tip = case.tracker.pr_head_sha(groups(case)[0].number)
    member = case.tracker.pr_head_sha(support.pr_for(case, 1).number)
    approved = case.store.load_approval(1).acceptance_blobs[own]
    assert case.git.blob_sha(member, own) == case.git.blob_sha(case.base, own) == approved
    assert case.git.blob_sha(tip, own) != approved
    assert not case.target_attempts and not case.merges and case.history.tip == case.base
    assert not case.store.owner_merge_facts() and not refs(case)
    retirement, = batch.retirements(case.ctx)
    assert "member #1" in retirement["reason"] and own in retirement["reason"]
    assert events(case)[-1]["reason"] == retirement["reason"]


@pytest.mark.parametrize("path,original", [
    ("machine.txt", "base\n"),
    ("src/runtime_config.py", None),
    ("src/runtime config\nquoted\".py", None),
], ids=["tracked", "untracked-source", "untracked-unusual-path"])
def test_train_retires_when_machine_checks_modify_the_tip_worktree(batched, path, original):
    case = batched
    update_base(case, {
        **({path: original} if original is not None else {}),
        "tests/test_machine_rewrite.py": (
            "from pathlib import Path\nimport pytest\n"
            "@pytest.mark.local_only\ndef test_machine_rewrite():\n"
            "    if all(Path(f'src/member_{n}.py').exists() for n in (5, 1)):\n"
            f"        Path({path!r}).write_text('fixed\\n')\n"
            "@pytest.mark.local_only\ndef test_machine_requirement():\n"
            "    if all(Path(f'src/member_{n}.py').exists() for n in (5, 1)):\n"
            f"        assert Path({path!r}).read_text() == 'fixed\\n'\n"),
    })
    queue_workers(case, [5, 1])
    support.gate(case)
    tip = case.tracker.pr_head_sha(groups(case)[0].number)
    if original is None:
        assert case.git.blob_sha(tip, path) is None
    else:
        assert git_command(case.root, "show", f"{tip}:{path}") == original.strip()
    assert not case.target_attempts and not case.merges and case.history.tip == case.base
    assert not case.store.owner_merge_facts() and not refs(case)
    retirement, = batch.retirements(case.ctx)
    assert "modified tracked files" in retirement["reason"]
    assert events(case)[-1]["reason"] == retirement["reason"]


def test_train_machine_checks_may_create_runner_artifacts(batched):
    case = batched
    update_base(case, {
        "tests/test_machine_artifact.py": (
            "from pathlib import Path\nimport pytest\n"
            "@pytest.mark.local_only\ndef test_machine_artifact():\n"
            "    artifact = Path('.foragent/machine-check.log')\n"
            "    artifact.parent.mkdir(exist_ok=True)\n"
            "    artifact.write_text('checked\\n')\n"),
    })
    queue_workers(case, [5, 1])
    support.gate(case)
    assert len(case.target_pushes) == 1 and not case.merges and not refs(case)
    assert len(case.store.owner_merge_facts()) == 2
    assert case.git.file_at(case.history.tip, ".foragent/machine-check.log") is None


def forged_tip(case, tip):
    """Retain the ledger/parents but add a file no member reviewed."""
    blob = git_command(case.root, "hash-object", "-w", "--stdin", input="Unreviewed\n")
    entries = git_command(case.root, "ls-tree", tip)
    tree = git_command(case.root, "mktree", input=f"{entries}\n100644 blob {blob}\tunreviewed.txt\n")
    parents = git_command(case.root, "show", "-s", "--format=%P", tip).split()
    message = git_command(case.root, "show", "-s", "--format=%B", tip)
    return git_command(case.root, "-c", "user.name=Other", "-c", "user.email=other@example.com",
                       "commit-tree", tree, "-p", parents[0], "-p", parents[1], "-m", message)


@pytest.mark.parametrize("boundary", ["recovery", "before-push"])
def test_train_tree_is_recomputed_from_base_and_heads_at_recovery_and_push(batched, monkeypatch, boundary):
    case = batched
    queue_workers(case, [5, 1])
    published_train(case, monkeypatch)
    pr = groups(case)[0]
    original = case.tracker.pr_head_sha(pr.number)
    tampered = forged_tip(case, original)

    def replace_tip():
        git_command(case.history.origin, "fetch", str(case.root), tampered)
        git_command(case.history.origin, "update-ref", f"refs/heads/{pr.head}", tampered, original)
        case.tracker.set_pr_head_sha(pr.number, tampered)

    if boundary == "recovery":
        replace_tip()
        with pytest.raises(ValueError, match="merge tree mismatch"):
            batch._decode(case.git, pr.head, tampered)
    else:
        ready = merge_owner._ready

        def tampered_after_final_check(ctx, place, job, member_pr, git, base, evidence):
            result = ready(ctx, place, job, member_pr, git, base, evidence)
            if place.n == 1:
                replace_tip()
                n, head, _, started = evidence.chain[-1]
                evidence.chain[-1] = (n, head, tampered, started)
            return result

        monkeypatch.setattr(merge_owner, "_ready", tampered_after_final_check)
    support.gate(case)
    assert not case.target_attempts and not case.target_pushes and not case.merges
    assert case.history.tip == case.base and not case.store.owner_merge_facts()
    assert "merge tree mismatch" in batch.retirements(case.ctx)[0]["reason"]


@pytest.mark.parametrize("change", ["head", "closed"])
def test_landed_member_completes_from_immutable_head_despite_mutable_pr_or_issue(batched, change):
    case = batched
    case.github_lag = True
    queue_workers(case, [5, 1])
    original = {n: case.tracker.pr_head_sha(support.pr_for(case, n).number) for n in (5, 1)}

    def crashed():
        raise KeyboardInterrupt("Lost response after the atomic push")

    case.after_target_push = crashed
    with pytest.raises(KeyboardInterrupt):
        support.gate(case)
    case.after_target_push = None
    if change == "head":
        updated = support.update_pr_branch(case, 5)
        assert not case.git.at(case.history.origin).is_ancestor(updated, "main")
    else:
        case.tracker.close_issue(5)
    support.gate(case)
    facts = case.store.owner_merge_facts()
    assert {fact["head"] for fact in facts.values()} == set(original.values())
    assert not merge_owner.admissions(case.store) and not merge_owner.pending(case.store, 5)
    assert len(case.target_pushes) == 1 and not case.merges
    assert case.tracker.get_pr(support.pr_for(case, 5).number).state == "open"


def test_combined_harness_checks_use_all_members_approved_files(batched, monkeypatch):
    case = batched
    update_base(case, {"conftest.py": "# Existing harness\n",
                      "tests/acceptance/test_machine.py":
                      "import pytest\n@pytest.mark.local_only\ndef test_machine():\n    pass\n"})
    approval = case.store.load_approval(1)
    body = approval.body.replace("- src/member_1.py", "- src/member_1.py\n- conftest.py")
    case.tracker.update_issue(1, title=case.tracker.get_issue(1).title, body=body)
    case.store.save_approval(**replace(approval, body=body, body_sha=body_hash(body)).__dict__)
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def authorized_harness(**kwargs):
        result = implement(**kwargs)
        if case.worker.number == 1:
            (kwargs["worktree"] / "conftest.py").write_text("# Approved harness edit by member 1\n")
        return result

    monkeypatch.setattr(model, "run", authorized_harness)
    queue_workers(case, [5, 1])
    support.gate(case)
    assert len(case.target_pushes) == 1 and not case.merges and not refs(case)
    assert len(case.store.owner_merge_facts()) == 2
    assert all(not case.tracker.get_issue(n).labels & {"blocked", "needs-human"} for n in (5, 1))


def test_prefix_scope_is_rechecked_at_the_locked_consumption_boundary(batched, monkeypatch):
    case = batched
    path = "tests/acceptance/test_interaction.py"
    update_base(case, {path: "from pathlib import Path\ndef test_interaction():\n"
        "    assert not (Path('src/member_5.py').exists() and Path('src/member_1.py').exists())\n"})
    queue_workers(case, [5, 1])
    body = case.store.load_approval(5).body.replace("tests/acceptance/test_own_5.py", path)
    case.tracker.add_issue(80, "Temporary owner", body, labels=["approved"])
    case.tracker.record_label(80, "approved", "human")
    case.store.save_approval(80, body=body, body_sha=body_hash(body),
                             acceptance_blobs={path: case.git.blob_sha(case.base, path)},
                             approved_by="human", approved_at=case.clock[0].isoformat())
    lock = StateStore.merge_lock

    @contextmanager
    def required_again(store):
        with lock(store):
            store.clear_approval(80)
            case.tracker.remove_labels(80, ["approved"])
            yield

    monkeypatch.setattr(StateStore, "merge_lock", required_again)
    support.gate(case)
    assert not case.target_pushes and not case.merges and not refs(case)
    assert events(case)[-1]["used_members"] == 0


def test_retired_attempt_is_not_rebuilt_after_flow_log_rotation(batched):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    case.tracker.set_pr_checks(groups(case)[0].number, "fail")
    support.gate(case)
    assert not refs(case) and events(case)[-1]["result"] == "unused"
    assert all(not row["spent"] for row in batch.retirements(case.ctx))
    for i in range(2048):
        case.store.record_flow("observation", index=i)
    assert not events(case)
    green(case)
    support.gate(case)
    assert len(groups(case)) == 1 and not case.target_pushes


def test_prefix_creation_identity_survives_temporary_listing_lag(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    find, get = case.tracker.find_pr, case.tracker.get_pr
    reads = []

    def direct_read(pr):
        reads.append(pr)
        return get(pr)

    monkeypatch.setattr(case.tracker, "get_pr", direct_read)
    monkeypatch.setattr(case.tracker, "find_pr", lambda branch: None if branch.startswith(batch.PREFIX) else find(branch))
    support.gate(case)
    assert len(groups(case)) == 1 and groups(case)[0].number in reads
    assert len(case.target_pushes) == 1 and len(case.store.owner_merge_facts()) == 2
    assert not refs(case)
    assert case.tracker.get_pr(groups(case)[0].number).state == "closed"
    support.gate(case)
    assert len(groups(case)) == 1 and len(case.target_pushes) == 1


@pytest.mark.parametrize("boundary", ["prepare", "consume", "cleanup"])
def test_batch_github_wait_reaches_owner_pause_without_retirement(batched, monkeypatch, boundary):
    case = batched
    queue_workers(case, [5, 1])
    published_train(case, monkeypatch)
    checks, reports = case.tracker.pr_checks, case.tracker.pr_acceptance_reports
    armed, failed = [boundary != "consume"], []

    def throttled(call, *args):
        if armed[0] and not failed:
            failed.append(True)
            raise TrackerError("HTTP 429 API rate limit exceeded")
        return call(*args)

    if boundary == "cleanup":
        case.tracker.set_pr_checks(groups(case)[0].number, "fail")
        monkeypatch.setattr(case.tracker, "pr_acceptance_reports", lambda head: throttled(reports, head))
    else:
        prefix_pr = groups(case)[0].number
        monkeypatch.setattr(case.tracker, "pr_checks", lambda pr: throttled(checks, pr) if pr == prefix_pr else checks(pr))
    if boundary == "consume":
        lock = StateStore.merge_lock

        @contextmanager
        def arm_at_consume(store):
            with lock(store):
                armed[0] = True
                yield

        monkeypatch.setattr(StateStore, "merge_lock", arm_at_consume)
    support.gate(case)
    assert failed and case.store.github_wait_until(case.clock[0]) is not None
    assert refs(case) and not events(case) and not case.target_pushes and not case.merges
    case.clock[0] += timedelta(minutes=2)
    case.tracker.set_pr_checks(groups(case)[0].number, "pass")
    support.drain(case, count=2)
    assert len(case.store.owner_merge_facts()) == 2 and not refs(case)


def test_existing_four_member_batch_survives_size_reduction(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1, 4, 2])
    published_train(case, monkeypatch)
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    support.gate(case)
    assert len(groups(case)) == len(case.target_pushes) == 1
    assert len(case.store.owner_merge_facts()) == 4 and not refs(case)


def test_last_reconciliation_throttle_stops_batch_preparation(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1, 4])
    get, failed = case.tracker.get_pr, []
    last = support.pr_for(case, 4).number

    def throttled(pr):
        if pr == last and not failed:
            failed.append(True)
            raise TrackerError("HTTP 429 API rate limit exceeded")
        return get(pr)

    monkeypatch.setattr(case.tracker, "get_pr", throttled)
    support.gate(case)
    assert failed and case.store.github_wait_until(case.clock[0]) is not None
    assert not case.batch_pushes and not groups(case) and not case.merges
    case.clock[0] += timedelta(minutes=2)
    support.drain(case, count=3)
    assert len(case.store.owner_merge_facts()) == 3


@pytest.mark.parametrize("outcome", ["pass", "fail", "deadline"])
@pytest.mark.parametrize("status", ["pending", "none", "reports"], ids=["queued", "registering", "artifacts"])
def test_pending_prefix_reserves_green_members_until_ci_or_deadline(batched, outcome, status):
    case = batched
    case.pending_batch = True
    support.queue_workers(case, [5, 1])
    support.gate(case)
    assert refs(case) and not case.merges and not case.target_pushes and not events(case)
    tip = groups(case)[0]
    case.tracker.set_pr_checks(tip.number, "pass" if status == "reports" else status)
    support.gate(case)  # A new owner recovers the same reservation.
    assert refs(case) and not case.merges and not case.target_pushes and not events(case)
    assert not batch.retirements(case.ctx) and len(case.batch_pushes) == 1
    if outcome == "deadline":
        case.clock[0] += timedelta(seconds=batch.DEADLINE_SECONDS)
    else:
        green(case)
        case.tracker.set_pr_checks(tip.number, outcome)
    support.gate(case)
    assert not refs(case) and not case.merges
    assert len(case.target_pushes) == (outcome == "pass")
    assert len(case.store.owner_merge_facts()) == (2 if outcome == "pass" else 0)
    assert events(case)[-1]["result"] == ("used" if outcome == "pass" else "unused")


def test_new_run_after_landed_train_does_not_erase_the_old_generations_merge_fact(batched):
    case = batched
    case.github_lag = True
    queue_workers(case, [5, 1])
    original = {n: case.store.get_run(n).started_at for n in [5, 1]}
    def crashed():
        raise KeyboardInterrupt("Owner stopped after landing")

    case.after_target_push = crashed
    with pytest.raises(KeyboardInterrupt):
        support.gate(case)
    case.after_target_push = None
    assert len(case.target_pushes) == 1 and not case.store.owner_merge_facts()
    case.clock[0] += timedelta(seconds=1)
    replacement = case.store.start_run(1, branch="new-followup", worktree=case.store.root / "followup")
    case.tracker.add_labels(1, ["running"])
    support.gate(case)
    assert not merge_owner.pending(case.store, 5)
    assert (1, original[1]) not in merge_owner.admissions(case.store)
    assert not case.merges and len(case.target_pushes) == 1
    case.github_lag = False
    support.gate(case)
    facts = case.store.owner_merge_facts()
    assert set(facts) == {(n, started) for n, started in original.items()}
    assert all(fact["merged_at"] for fact in facts.values())
    assert case.store.get_run(1).started_at == replacement.started_at
    assert case.store.get_run(1).status == "running"
    assert case.tracker.get_issue(1).state == "open"
    assert "running" in case.tracker.get_issue(1).labels
    assert not merge_owner.admissions(case.store)


def test_member_final_read_throttle_keeps_train_unspent_and_stops_landing(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1, 4])
    lock, get = StateStore.merge_lock, case.tracker.get_pr
    armed, failed = [], []
    second = support.pr_for(case, 1).number

    @contextmanager
    def armed_under_lock(store):
        with lock(store):
            armed.append(True)
            yield

    def throttled(pr):
        if armed and pr == second and not failed:
            failed.append(True)
            raise TrackerError("HTTP 429 API rate limit exceeded")
        return get(pr)

    monkeypatch.setattr(StateStore, "merge_lock", armed_under_lock)
    monkeypatch.setattr(case.tracker, "get_pr", throttled)
    support.gate(case)
    assert failed and case.store.github_wait_until(case.clock[0]) is not None
    assert not case.target_pushes and not case.merges and not case.store.owner_merge_facts()
    assert refs(case) and not batch.retirements(case.ctx) and not events(case)
    assert all(case.tracker.get_issue(n).state == "open" for n in [5, 1, 4])
    case.clock[0] += timedelta(minutes=2)
    support.gate(case)
    assert len(case.target_pushes) == 1 and len(case.store.owner_merge_facts()) == 3


def test_worktree_fetch_outage_retires_attempt_without_spending_members(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    head = case.tracker.pr_head_sha(support.pr_for(case, 5).number)
    transport, failed = case.git._run, []

    def unavailable(argv):
        if list(argv) == ["fetch", "origin", head] and not failed:
            failed.append(True)
            raise GitError("member fetch unavailable")
        return transport(argv)

    monkeypatch.setattr(case.git, "_run", unavailable)
    support.gate(case)
    assert failed and not case.target_pushes and not case.merges and not refs(case)
    assert batch.retirements(case.ctx) and all(not row["spent"] for row in batch.retirements(case.ctx))
    assert "fetch unavailable" in events(case)[-1]["reason"]
    support.drain(case, count=2)
    assert len(case.store.owner_merge_facts()) == 2


def test_stale_unlanded_admission_preserves_the_new_running_generation(batched):
    case = batched
    queue_workers(case, [1])
    original = case.store.get_run(1).started_at
    case.clock[0] += timedelta(seconds=1)
    replacement = case.store.start_run(1, branch="new-followup", worktree=case.store.root / "followup")
    case.tracker.add_labels(1, ["running"])
    support.gate(case)
    assert not case.merges and not case.target_pushes
    assert (1, original) not in merge_owner.admissions(case.store)
    assert case.store.get_run(1).started_at == replacement.started_at
    assert case.store.get_run(1).status == "running"
    assert "running" in case.tracker.get_issue(1).labels


def test_union_refresh_checks_and_publishes_its_own_tree_before_ci_reuse(batched, monkeypatch):
    case = batched
    update_base(case, {".gitattributes": "shared.txt merge=union\n", "shared.txt": "anchor\n",
        "tests/test_union_machine.py": "import pytest\nfrom pathlib import Path\n"
        "@pytest.mark.local_only\ndef test_union_machine():\n"
        "    assert Path('shared.txt').read_text() != 'anchor\\na\\nb\\n'\n"})
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def append_member(**kwargs):
        result = implement(**kwargs)
        (kwargs["worktree"] / "shared.txt").write_text("anchor\n" + ("a\n" if case.worker.number == 5 else "b\n"))
        return result

    monkeypatch.setattr(model, "run", append_member)
    queue_workers(case, [5, 1])
    support.gate(case)
    assert not case.target_pushes and not case.merges and not refs(case)
    assert not case.store.owner_merge_facts()
    assert events(case)[-1]["used_members"] == 0
    assert all(not row["spent"] for row in batch.retirements(case.ctx))


def test_combined_conflict_markers_are_rejected_by_the_normal_refresh_publication_gate(batched, monkeypatch):
    case = batched
    update_base(case, {"shared.txt": "anchor\nmiddle\nend\n"})
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def markers(**kwargs):
        result = implement(**kwargs)
        content = "anchor\nmiddle\nend\n"
        content = "<<<<<<< HEAD\n" + content if case.worker.number == 5 else content + ">>>>>>> branch\n"
        (kwargs["worktree"] / "shared.txt").write_text(content)
        return result

    monkeypatch.setattr(model, "run", markers)
    queue_workers(case, [5, 1])
    support.gate(case)
    assert not case.target_pushes and not case.merges and not refs(case)
    assert not case.store.owner_merge_facts()
    assert events(case)[-1]["used_members"] == 0
    assert all(not row["spent"] for row in batch.retirements(case.ctx))
