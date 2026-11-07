"""Merge-owner batches on real Git and measured tip CI: train CI covers every member, and the tip tree is checked at each boundary.

The offline transport uses fetch/update-ref, never forbidden Git commands.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest

from conftest import git_command
from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import merge_owner, merge_owner_batch as batch, orch_queue, runner
from foragent.git import GitError
from foragent.issue import body_hash, parse_body
from foragent.scheduler import TickReport
from foragent.state import StateStore
from foragent.tracker import GhTracker, TrackerError
from helpers.merge_batch import (  # noqa: F401
    batch_candidates, batched, composed_numbers, events, green, groups, published_train, queue_workers, refs, update_base,
)


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
    case.tracker.set_pr_checks(groups(case)[-1].number, "pending")
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
    assert len(case.store.owner_merge_facts()) == 2
    assert len(case.merges) == len(case.target_pushes) == 1
    assert case.target_pushes[0][0] != pr.head_sha
    assert case.git.remote_branch_sha(pr.head) == pr.head_sha


def test_train_related_units_use_published_tip_receipt_and_keep_locked_commands(batched, monkeypatch):
    from foragent import runner

    case = batched
    paths = [f"tests/test_member_{n}.py" for n in (5, 1)]
    update_base(case, {path: "def test_member():\n    pass\n" for path in paths})
    queue_workers(case, [5, 1])
    member_heads = {case.tracker.pr_head_sha(support.pr_for(case, n).number) for n in (5, 1)}
    lookups, commands = [], []

    def receipt(head):
        lookups.append(head)
        return 42, {f"{path}::test_member" for path in paths}

    check = runner._check

    def observed(command, *args, **kwargs):
        commands.append(command)
        return check(command, *args, **kwargs)

    monkeypatch.setattr(case.tracker, "pr_unit_passed_tests", receipt)
    monkeypatch.setattr(runner, "_check", observed)
    support.gate(case)

    tip = case.tracker.pr_head_sha(groups(case)[0].number)
    assert lookups and set(lookups) == {tip}
    assert not member_heads.intersection(lookups)
    assert len(case.target_pushes) == 1 and len(case.store.owner_merge_facts()) == 2
    assert not any("--junitxml " in command for command in commands)
    assert any("--collect-only" in command and all(path in command for path in paths)
               for command in commands)
    for n in (5, 1):
        assert any(f"tests/acceptance/test_own_{n}.py" in command and "--collect-only" not in command
                   for command in commands)
    assert f"related units: all 2 passed in CI run 42 for {tip}" in case.store.log_path(5).read_text()


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
    """The locked recheck uses the scope fixed at build time (#2100).

    #80 owned the interaction test when the batch was built, so CI measured
    without it. #80 losing its approval under the lock does not make that
    test count as lost: the batch lands once instead of retiring.
    """
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
    tip = case.history.tip
    assert path in batch._recorded_exclusions(case.git, tip)
    assert case.target_pushes == [(tip, "refs/heads/main", case.base)] and not refs(case)
    assert not any("scope" in row.get("reason", "") for row in batch.retirements(case.ctx))
    assert events(case)[-1]["used_members"] == 2


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
    assert len(groups(case)) == 2 and len(case.target_pushes) == 1
    assert composed_numbers(case) == [5]
    assert len(case.store.owner_merge_facts()) == 1


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


def test_running_tip_at_61_minutes_keeps_reservation_and_then_lands(batched):
    case = batched
    case.pending_batch = True
    support.queue_workers(case, [5, 1])
    support.gate(case)
    born = case.clock[0]
    tip = case.tracker.pr_head_sha(groups(case)[0].number)
    case.clock[0] += timedelta(minutes=45)
    green(case)
    proof = batch.prepare(case.ctx, TickReport(), batch_candidates(case))
    assert isinstance(proof, batch.Evidence)  # Finished CI is observed without starting member checks.
    case.tracker.set_pr_checks(groups(case)[0].number, "pending")
    case.clock[0] = born + timedelta(minutes=61)
    support.gate(case)
    assert refs(case) and not events(case) and not batch.retirements(case.ctx)
    assert len(case.batch_pushes) == 1 and not case.target_pushes
    green(case)
    support.gate(case)
    assert case.history.tip == tip and len(case.target_pushes) == 1
    assert not refs(case) and events(case)[-1]["reason"] == "train merged"


@pytest.mark.parametrize("status", ["pending", "none", "pass"], ids=["queued", "registering", "artifacts"])
def test_no_progress_for_60_minutes_retires_with_silence_reason(batched, status):
    case = batched
    case.pending_batch = True
    support.queue_workers(case, [5, 1])
    support.gate(case)
    tip_pr = groups(case)[0]
    progress_path = case.store.root / "merge-batch" / f"{case.tracker.pr_head_sha(tip_pr.number)}.json"
    progressed_at = datetime.fromtimestamp(json.loads(progress_path.read_text())["last_progress"],
                                          tz=case.clock[0].tzinfo)
    case.tracker.set_pr_checks(tip_pr.number, status)
    support.gate(case)
    # gate advances one second; check immediately before and at the boundary.
    case.clock[0] = progressed_at + timedelta(seconds=batch.SILENCE_SECONDS - 2)
    support.gate(case)
    assert refs(case) and not events(case)
    support.gate(case)
    retirement, = batch.retirements(case.ctx)
    assert "silence" in retirement["reason"] and "60 minutes" in retirement["reason"]
    assert not retirement["spent"] and not refs(case) and not case.target_pushes


def test_repeated_green_ci_without_reports_does_not_renew_silence(batched):
    case = batched
    case.pending_batch = True
    support.queue_workers(case, [5, 1])
    support.gate(case)
    case.tracker.set_pr_checks(groups(case)[0].number, "pass")
    support.gate(case)
    for _ in range(3):
        case.clock[0] += timedelta(minutes=15)
        support.gate(case)
        assert refs(case) and not events(case)
    case.clock[0] += timedelta(minutes=15)
    support.gate(case)
    assert not refs(case) and "silence" in events(case)[-1]["reason"]


def test_completed_member_checks_keep_green_batch_alive_past_birth_silence_window(batched, monkeypatch):
    case = batched
    case.pending_batch = True
    support.queue_workers(case, [5, 1])
    support.gate(case)
    born = case.clock[0]
    tip = case.tracker.pr_head_sha(groups(case)[0].number)
    case.clock[0] += timedelta(minutes=45)
    green(case)
    commands = {command: n for n in (5, 1) for command in
                parse_body(case.store.load_approval(n).body).done_when_commands}
    run, completed = runner.run_logged, []

    def measured(argv, **kwargs):
        result = run(argv, **kwargs)
        if len(argv) == 3 and argv[:2] == ["/bin/sh", "-c"] and result[:2] == (0, False):
            for command, n in commands.items():
                if command in argv[2]:
                    case.clock[0] += timedelta(minutes=14)
                    completed.append((n, case.clock[0]))
        return result

    monkeypatch.setattr(runner, "run_logged", measured)
    support.gate(case)
    assert [n for n, _ in completed] == [5, 1]
    instants = [born, *(at for _, at in completed)]
    assert all(after - before < timedelta(seconds=batch.SILENCE_SECONDS)
               for before, after in zip(instants, instants[1:]))
    assert case.clock[0] - born > timedelta(seconds=batch.SILENCE_SECONDS)
    assert case.history.tip == tip and len(case.target_pushes) == 1
    assert not refs(case) and events(case)[-1]["reason"] == "train merged"


@pytest.mark.parametrize("change", ["target", "member-head", "tip-ci"])
def test_invalidation_after_member_check_stops_remaining_owner_checks(batched, monkeypatch, change):
    case = batched
    case.pending_batch = True
    support.queue_workers(case, [5, 1])
    support.gate(case)
    green(case)
    verify, checked = merge_owner._verify_review_head, []

    def invalidate(n, *args, **kwargs):
        result = verify(n, *args, **kwargs)
        checked.append(n)
        assert result is None
        if change == "target":
            case.history.update({"src/neighbor.py": "VALUE = 'changed during owner checks'\n"})
        elif change == "member-head":
            support.update_pr_branch(case, 1)
        else:
            case.tracker.set_pr_checks(groups(case)[0].number, "fail")
        return result

    monkeypatch.setattr(merge_owner, "_verify_review_head", invalidate)
    support.gate(case)
    assert checked == [5]
    assert not refs(case) and not case.target_pushes and not case.merges
    retirement, = batch.retirements(case.ctx)
    assert retirement["reason"]
    if change == "member-head":
        assert "member #1" in retirement["reason"]
    elif change == "tip-ci":
        assert retirement["reason"] == "tip CI red"


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


def test_recorded_exclusions_come_from_the_tip_commit_only(tmp_path):
    """The judged scope is the list written into the tip, never a parent's list."""
    from foragent.git import GitRepo

    git_command(tmp_path, "init", "-q")
    for key, value in (("user.name", "t"), ("user.email", "t@t")):
        git_command(tmp_path, "config", key, value)

    tree = git_command(tmp_path, "mktree", input="")

    def commit(message):
        return git_command(tmp_path, "commit-tree", tree, "-m", message)

    base = commit('Base\n\nForagent-Acceptance-Excluded: ["tests/acceptance/test_base.py"]')
    plain = commit("Member\n\nForagent-Member: #1 abc")
    tip = commit('Member\n\nForagent-Member: #2 def\nForagent-Acceptance-Excluded: ["a.py","b.py"]')
    broken = commit('Member\n\nForagent-Acceptance-Excluded: ["b.py","a.py"]')
    git = GitRepo(tmp_path)
    assert batch._recorded_exclusions(git, tip) == ["a.py", "b.py"]
    assert batch._recorded_exclusions(git, base) == ["tests/acceptance/test_base.py"]
    assert batch._recorded_exclusions(git, plain) is None  # published before the list was recorded
    with pytest.raises(ValueError, match="tip exclusions trailer invalid"):
        batch._recorded_exclusions(git, broken)


def test_tip_outcome_ignores_the_moving_open_issue_set(batched, monkeypatch):
    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    support.gate(case)
    pr = groups(case)[0]
    tip = case.tracker.pr_head_sha(pr.number)
    base, chain = batch._decode(case.git, pr.head, tip)
    green(case)
    born = int(case.clock[0].timestamp())
    proof = batch.Evidence(base, chain, pr.number, born, batch._recorded_exclusions(case.git, tip))
    assert proof.excluded and proof.outcome(case.ctx) == "pass"
    monkeypatch.setattr(batch, "_exclusions", lambda *a: pytest.fail("recomputed from open issues"))
    assert proof.outcome(case.ctx) == "pass"
