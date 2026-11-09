"""Merge-owner batches on real Git and measured tip CI: only the tip is published, and it lands only through every fresh single-path check.

The offline transport uses fetch/update-ref, never forbidden Git commands.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

import pytest

from conftest import git_command
from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import ci_local, ci_mode, merge_owner, merge_owner_batch as batch, orch_queue
from foragent.config import load_config
from foragent.git import GitError
from foragent.scheduler import TickReport
from foragent.state import StateStore
from helpers.merge_batch import (  # noqa: F401
    batch_candidates, batched, composed_numbers, edit_member_workflows, events, green, groups, published_train,
    queue_workers, refs,
)



@pytest.mark.parametrize("numbers", [[5, 1], [2, 4, 1, 5], [5, 1, 4, 2, 6, 3]])
def test_only_tip_published_and_reused_by_single_path_across_restart(request, monkeypatch, numbers):
    if len(numbers) > 4:
        from helpers import merge_world as contract
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


def test_member_already_in_prefix_retires_batch_without_excluding_its_checks(batched, monkeypatch):
    case = batched
    # Worker worktrees share this repository's objects and local Git identity.
    git_command(case.root, "config", "--local", "user.name", "Offline Tests")
    git_command(case.root, "config", "--local", "user.email", "tests@example.com")
    queue_workers(case, [1])
    ancestor = case.tracker.pr_head_sha(support.pr_for(case, 1).number)
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def with_dependency(**kwargs):
        if case.worker.number == 5:
            git_command(kwargs["worktree"], "merge", "--no-commit", "--no-ff", ancestor)
        return implement(**kwargs)

    monkeypatch.setattr(model, "run", with_dependency)
    queue_workers(case, [5, 4])
    orch_queue.set_global(case.store, [5, 1, 4])
    dependent = case.tracker.pr_head_sha(support.pr_for(case, 5).number)
    assert case.git.is_ancestor(ancestor, dependent)
    case.tracker.set_pr_checks(support.pr_for(case, 1).number, "fail")
    case.tracker.set_pr_checks(support.pr_for(case, 5).number, "pending")

    report = support.gate(case)

    assert not case.target_attempts and not case.target_pushes and not case.merges, report.actions
    assert case.history.tip == case.base and not case.store.owner_merge_facts()
    assert not case.batch_pushes and not groups(case) and not refs(case)
    assert not any("#1 merge train excluded" in action for action in report.actions)
    retired, = batch.retirements(case.ctx)
    assert retired["reason"] == "member already reachable" and retired["spent"] == []
    assert events(case)[-1]["reason"] == "member already reachable"
    assert events(case)[-1]["result"] == "unused"
    assert {n for n, _ in merge_owner.admissions(case.store)} == {5, 1, 4}


@pytest.mark.parametrize("path", [".github/workflows/foragent-acceptance.yml",
                                 ".github/workflows/foragent-acceptance-extra.yaml"])
def test_workflow_changing_member_lands_alone_on_pr_ci_while_remaining_members_batch(batched, monkeypatch, path):
    case = batched
    edit_member_workflows(case, monkeypatch, {5: path})
    queue_workers(case, [5, 1, 4])
    changed_head = case.tracker.pr_head_sha(support.pr_for(case, 5).number)
    fallback, measured = ci_local.fallback_comparison, []

    def local(attempt, base_sha, **kwargs):
        measured.append(attempt.n)
        return fallback(attempt, base_sha, **kwargs)

    monkeypatch.setattr(ci_local, "fallback_comparison", local)
    support.gate(case)
    tip, = groups(case)
    _, chain = batch._decode(case.git, tip.head, case.tracker.pr_head_sha(tip.number))
    assert [n for n, *_ in chain] == [1, 4]
    assert all(head != changed_head for _, head, *_ in chain)
    assert len(case.target_pushes) == 1 and not case.merges
    support.drain(case, count=3)
    # The member's own workflow edit selects the whole suite: PR CI compares it, not a local baseline.
    assert measured == []
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
    assert failed and len(groups(case)) == len(case.target_pushes) == 1
    assert composed_numbers(case) == [1] and not case.merges
    assert any("workflow diff unavailable" in action for action in report.actions)
    support.drain(case, count=2)
    assert len(case.target_pushes) == 2 and not case.merges
    assert len(case.store.owner_merge_facts()) == 2


def test_remote_only_candidate_does_not_block_locally_available_batch(batched, monkeypatch, tmp_path):
    case = batched
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    queue_workers(case, [5, 1, 4])
    candidates = batch_candidates(case)
    for _, job, _ in candidates:
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
    candidates = [candidates[2], *candidates[:2]]
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
    from helpers import merge_world as contract

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
    assert len(case.merges) == len(case.target_pushes) == 1
    assert composed_numbers(case) == [1]
    assert len(case.store.owner_merge_facts()) == 2


def test_target_moving_under_lock_is_rejected_even_when_its_tree_is_identical(batched, monkeypatch):
    case = batched
    queue_workers(case, [5, 1])
    lock = StateStore.merge_lock

    @contextmanager
    def moved(store):
        with lock(store):
            # The net tree is identical, but each intervening commit matters.
            case.history.update({"src/transient.py": "VALUE = 1\n"})
            case.history.update({"src/transient.py": None})
            yield

    monkeypatch.setattr(StateStore, "merge_lock", moved)
    support.gate(case)
    assert not case.target_pushes and not case.merges and not refs(case)
    assert events(case)[-1]["used_members"] == 0
    assert all(not row["spent"] for row in batch.retirements(case.ctx))


def _culprit_world(*, checks="pass", measured="base", culprit=True, landing="T1"):
    """PM r1039: a green batch [5, 1] whose final check named #5; #1 remains."""
    from types import SimpleNamespace as NS

    row = dict(reason="member #5: CI failed at final merge recheck", passed=True, base="base", culprit=culprit,
               members=[dict(n=5, head="h5", started_at="s5"), dict(n=1, head="h1", started_at="s1")],
               spent=[dict(n=5, head="h5", started_at="s5")])
    tracker = NS(pr_checks=lambda pr: checks,
                 pr_acceptance_reports=lambda head: [{"report": {"base_sha": measured}}])
    trees = {"merge-tree": landing, "show": "T1"}  # h1's own tree is the one PR CI measured.
    ctx = NS(git=NS(is_ancestor=lambda head, base: False, _call=lambda argv: trees[argv[0]]), tracker=tracker)
    member = (NS(n=1, started_at="s1"), {"head": "h1"}, NS(head_sha="h1", number=9001))
    other = (NS(n=4, started_at="s4"), {"head": "h4"}, NS(head_sha="h4", number=9004))
    return ctx, [row], [member, other], member


def test_final_check_culprit_remainder_verified_on_current_main_is_direct():
    ctx, retired, candidates, member = _culprit_world()
    assert batch._verified_remainder(ctx, retired, candidates, "base") == member


@pytest.mark.parametrize("change", [
    dict(checks="pending"), dict(checks="fail"), dict(measured="older-main"), dict(culprit=False),
    dict(landing="T-main-plus-h1"),
])
def test_final_check_culprit_remainder_without_current_proof_is_not_direct(change):
    ctx, retired, candidates, _ = _culprit_world(**change)
    assert batch._verified_remainder(ctx, retired, candidates, "base") is None


def test_final_check_culprit_with_two_remaining_members_is_not_direct():
    ctx, retired, candidates, _ = _culprit_world()
    retired[-1]["members"].append(dict(n=4, head="h4", started_at="s4"))
    assert batch._verified_remainder(ctx, retired, candidates, "base") is None


def _two_commit_main(tmp_path):
    """main = base; h1 adds member_1 on base; main then advances without conflict (plumbing only)."""
    from foragent.git import GitRepo

    root = tmp_path / "repo"
    root.mkdir()
    git_command(root, "init", "-q", "-b", "main")
    env = ["-c", "user.email=t@example.com", "-c", "user.name=t"]

    def commit(files, *parents):
        for name, text in files.items():
            (root / name).write_text(text)
        git_command(root, "add", *files)
        tree = git_command(root, "write-tree")
        return git_command(root, *env, "commit-tree", tree, *(a for p in parents for a in ("-p", p)), "-m", "c")

    base = commit({"shared.py": "VALUE = 0\n"})
    head = commit({"member_1.py": "VALUE = 1\n"}, base)
    git_command(root, "rm", "-q", "--cached", "member_1.py")
    moved = commit({"shared.py": "VALUE = 2\n"}, base)
    return GitRepo(root), base, head, moved


def test_member_ci_proves_landing_only_when_merge_keeps_the_measured_head_tree(tmp_path):
    """Review r2552: CI re-run on B's older head names the new main but measured only B's tree."""
    git, base, head, moved = _two_commit_main(tmp_path)
    assert batch._head_tree_lands(git, base, head)
    assert not batch._head_tree_lands(git, moved, head), "main+B is a tree PR CI never measured"


def test_member_evidence_refuses_a_chain_tip_that_is_not_the_measured_head_tree(tmp_path):
    from types import SimpleNamespace as NS

    git, base, head, moved = _two_commit_main(tmp_path)
    tip = git._call(["-c", "user.email=t@example.com", "-c", "user.name=t", "commit-tree", git._call(["merge-tree", "--write-tree", moved, head]).splitlines()[0],
                     "-p", moved, "-p", head, "-m", "Merge member #1"]).strip()
    tracker = NS(get_pr=lambda pr: NS(head_sha=head, base="main", state="open", merged_at=None),
                 pr_checks=lambda pr: "pass",
                 pr_acceptance_reports=lambda h: [{"report": {"base_sha": moved}}])
    store = NS(root=tmp_path / "state", _write_json=lambda path, data: None)
    ctx = NS(git=git, tracker=tracker, config=NS(base_branch="main"), store=store,
             now=lambda: datetime(2026, 11, 7))
    proof = batch.MemberEvidence(moved, [(1, head, tip, "s1")], None, 0, [], member_pr=9001)
    with pytest.raises(ValueError, match="landing tree"):
        proof.reports(ctx)
    same = git._call(["-c", "user.email=t@example.com", "-c", "user.name=t", "commit-tree", git._call(["show", "-s", "--format=%T", head]).strip(),
                      "-p", base, "-p", head, "-m", "Merge member #1"]).strip()
    tracker.pr_acceptance_reports = lambda h: [{"report": {"base_sha": base}}]
    proof = batch.MemberEvidence(base, [(1, head, same, "s1")], None, 0, [], member_pr=9001)
    assert proof.reports(ctx)[2] == head
