"""D89 and Jin 2026-11-05: useful owner work, cancellation and fair progress.

Use the existing real runner/scheduler/Git/state/measured-CI world. The command
transport below runs the real approved checks, then advances only the fake
clock. It does not replace a verification or a merge decision.

The unspecified silence window is taken to be today's DEADLINE_SECONDS. CI
keeps that existing wait limit; green owner work is governed by silence instead.
"""

from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from acceptance import test_merge_path_real_settings as contract  # noqa: E402
from acceptance.test_merge_path_real_settings import world  # noqa: E402, F401
from test_merge_owner_proto import proto  # noqa: E402, F401
import test_merge_owner_proto as support  # noqa: E402
from test_merge_owner_batch import (  # noqa: E402, F401
    batched, edit_member_workflows, events, green, groups, refs, update_base,
)
from foragent import merge_owner_batch as batch, orch_queue, runner  # noqa: E402
from foragent.issue import body_hash, parse_body  # noqa: E402
from foragent.orch import merge  # noqa: E402
from foragent.state import StateStore  # noqa: E402

SILENCE = timedelta(seconds=batch.DEADLINE_SECONDS)


def waiting_batch(case, members):
    case.pending_batch = True
    support.queue_workers(case, members)
    support.gate(case)
    pr, = groups(case)
    tip = case.tracker.pr_head_sha(pr.number)
    assert refs(case) and not events(case) and not batch.retirements(case.ctx)
    assert not case.target_pushes and not case.merges
    return case.history.tip, tip


def observe_member_checks(case, monkeypatch, members, *, starting=lambda n: None,
                          finished=lambda n: None):
    """Identify commands by the approved invocation, retaining their real results."""
    commands = {command: n for n in members for command in
                parse_body(case.store.load_approval(n).body).done_when_commands}
    run = runner.run_logged
    completed = []

    def measured(argv, **kwargs):
        n = commands.get(argv[2]) if len(argv) == 3 and argv[:2] == ["/bin/sh", "-c"] else None
        if n is not None:
            starting(n)
        result = run(argv, **kwargs)
        if n is not None and result[0] == 0 and not result[1]:
            finished(n)
            completed.append((n, case.clock[0]))
        return result

    monkeypatch.setattr(runner, "run_logged", measured)
    return completed


def assert_unused(case, base, tip):
    assert not case.target_attempts and not case.target_pushes and not case.merges
    assert case.history.tip == base and not case.store.owner_merge_facts()
    assert not refs(case), "A retired batch must release its published reservation"
    retirement, = batch.retirements(case.ctx)
    assert retirement["candidate"] == tip and retirement["reason"]
    event, = events(case)
    assert event["result"] == "unused" and event["used_members"] == 0
    assert event["reason"] == retirement["reason"]
    return retirement


@pytest.mark.parametrize("member_count", [2, 6], ids=["past-one-deadline", "past-two-deadlines"])
def test_progressing_green_batch_lands_once_past_elapsed_ci_deadlines(
        request, monkeypatch, member_count):
    members = [5, 1, 4, 2, 6, 3][:member_count]
    monkeypatch.setattr(contract, "NUMBERS", tuple(members))
    case = request.getfixturevalue("batched")
    # Keep the independent per-turn runtime budget out of this batch-age test.
    case.config = replace(case.config, merge_batch_size=member_count, max_runtime_minutes=24 * 60)
    case.ctx.config = case.config
    base, tip = waiting_batch(case, members)
    built_at = case.clock[0]
    case.clock[0] += SILENCE * 3 / 4  # CI passes within its existing wait allowance.
    green(case)
    completed = observe_member_checks(
        case, monkeypatch, members,
        finished=lambda n: case.clock.__setitem__(0, case.clock[0] + timedelta(minutes=14)),
    )
    support.gate(case)

    # Every actual member check passed, with no silent gap reaching the window.
    assert [n for n, _ in completed] == members
    instants = [built_at, *(at for _, at in completed)]
    assert all(after - before < SILENCE for before, after in zip(instants, instants[1:]))
    assert case.clock[0] - built_at > SILENCE * (1 if member_count == 2 else 2)
    assert case.target_pushes == [(tip, "refs/heads/main", base)], (
        "Completed member checks must renew a green batch until it lands; "
        f"retired instead: {batch.retirements(case.ctx)}"
    )
    assert case.history.tip == tip and not case.merges and not refs(case)
    assert {n for n, _ in case.store.owner_merge_facts()} == set(members)
    assert all(case.tracker.get_issue(n).state == "closed" for n in members)
    event, = events(case)
    assert event["result"] == "used" and event["used_members"] == member_count
    assert all(row["reason"] == "train merged" for row in batch.retirements(case.ctx))
    support.gate(case)
    assert case.target_pushes == [(tip, "refs/heads/main", base)], "A replay must not land twice"
    assert len(case.store.owner_merge_facts()) == member_count


def test_silent_green_batch_retires_and_its_members_can_land_in_later_batches(batched, monkeypatch):
    case = batched
    base, tip = waiting_batch(case, [5, 1])
    green(case)
    with monkeypatch.context() as patch:
        def interrupt_next_check(n):
            if completed:
                raise KeyboardInterrupt("Owner disappeared before its next check completed")

        completed = observe_member_checks(
            case, patch, [5, 1], starting=interrupt_next_check,
            finished=lambda n: case.clock.__setitem__(0, case.clock[0] + timedelta(minutes=10)),
        )
        with pytest.raises(KeyboardInterrupt, match="Owner disappeared"):
            support.gate(case)
    assert len(completed) == 1 and refs(case) and not events(case)
    case.clock[0] = completed[-1][1] + SILENCE + timedelta(seconds=1)
    support.gate(case)  # A fresh owner sees silence, even though CI remains green.
    retired = assert_unused(case, base, tip)
    assert not retired["spent"], "Silence must not spend the members' publications"
    assert all(case.tracker.get_issue(n).state == "open" for n in (5, 1))

    # The old identity stays retired; both original members join different batches.
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    case.pending_batch = True
    # The shared fixture freezes Git dates at setup. New batches are born now.
    for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(key, case.clock[0].isoformat())
    support.queue_workers(case, [4, 2])
    orch_queue.set_global(case.store, [5, 4, 1, 2])
    support.gate(case)
    assert refs(case) and len(groups(case)) == 2
    assert case.tracker.pr_head_sha(groups(case)[-1].number) != tip
    green(case)
    support.drain(case)
    assert len(case.target_pushes) == 2 and not case.merges and not refs(case)
    assert {n for n, _ in case.store.owner_merge_facts()} == {5, 1, 4, 2}
    assert all(case.tracker.get_issue(n).state == "closed" for n in (5, 1, 4, 2))
    assert [event["result"] for event in events(case)] == ["unused", "used", "used"]


@pytest.mark.parametrize("status", ["pending", "none", "reports"],
                         ids=["pending-ci", "registering-ci", "pending-artifacts"])
def test_pending_tip_ci_keeps_its_wait_limit_despite_repeated_polls(batched, status):
    case = batched
    base, tip = waiting_batch(case, [5, 1])
    case.tracker.set_pr_checks(groups(case)[0].number, "pass" if status == "reports" else status)
    for _ in range(3):
        case.clock[0] += SILENCE / 4
        support.gate(case)
        assert refs(case) and len(groups(case)) == 1 and not events(case)
        assert not batch.retirements(case.ctx) and not case.target_pushes and not case.merges
    case.clock[0] += SILENCE / 4
    support.gate(case)
    retired = assert_unused(case, base, tip)
    assert not retired["spent"]


@pytest.mark.parametrize("change", ["target", "tip-ci", "member-head"])
def test_invalidating_event_during_green_owner_checks_retires_at_once(batched, monkeypatch, change):
    case = batched
    base, tip = waiting_batch(case, [5, 1])
    green(case)
    changes = []

    def invalidate(n):
        if changes:
            return
        changes.append(change)
        if change == "target":
            case.history.update({"src/neighbor.py": "VALUE = 'target moved'\n"})
        elif change == "tip-ci":
            case.tracker.set_pr_checks(groups(case)[0].number, "fail")
        else:
            support.update_pr_branch(case, 1)

    completed = observe_member_checks(case, monkeypatch, [5, 1], finished=invalidate)
    support.gate(case)
    assert changes == [change] and completed, "Inject the event after real owner work"
    retired = assert_unused(case, case.history.tip if change == "target" else base, tip)
    if change == "tip-ci":
        assert retired["reason"] == "tip CI red"
    elif change == "member-head":
        assert "member #1" in retired["reason"]
    else:
        assert case.history.tip != base and case.history.tip != tip
    assert all(case.tracker.get_issue(n).state == "open" for n in (5, 1))


def test_green_batch_with_changed_approved_acceptance_file_retires_at_once(batched, monkeypatch):
    case = batched
    own = "tests/acceptance/test_own_1.py"
    approval = case.store.load_approval(5)
    body = approval.body.replace("- src/member_5.py", f"- src/member_5.py\n- {own}")
    case.tracker.update_issue(5, title=case.tracker.get_issue(5).title, body=body)
    case.store.save_approval(**replace(approval, body=body, body_sha=body_hash(body)).__dict__)
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def edit_other_members_lock(**kwargs):
        result = implement(**kwargs)
        if case.worker.number == 5:
            path = kwargs["worktree"] / own
            path.write_text(path.read_text() + "\n# Changed after member #1 approved these bytes.\n")
        return result

    monkeypatch.setattr(model, "run", edit_other_members_lock)
    case.pending_batch = True
    support.queue_workers(case, [1])
    case.worker.number = 5
    case.clock[0] += timedelta(seconds=1)
    case.store.start_run(5, branch="foragent/issue-5", worktree=case.store.worktree_path(5))
    case.tracker.add_labels(5, ["running"])
    assert runner.run_issue(5, case.ctx) == "held"
    case.clock[0] += timedelta(seconds=1)
    assert "merge-queued" in merge(case.ctx, support.pr_for(case, 5).number, approved_by="human")
    orch_queue.set_global(case.store, [5, 1])
    support.gate(case)
    pr, = groups(case)
    tip = case.tracker.pr_head_sha(pr.number)
    assert refs(case) and not events(case)
    green(case)  # The edit preserves all test outcomes, but changes approved bytes.
    support.gate(case)
    retired = assert_unused(case, case.base, tip)
    assert "member #1" in retired["reason"] and own in retired["reason"]


def observe_owner_commands(monkeypatch, *, finished=lambda command, root: None):
    """Observe real executions, including invocations other than Done when."""
    run = runner.run_logged
    commands = []

    def measured(argv, **kwargs):
        result = run(argv, **kwargs)
        if len(argv) == 3 and argv[:2] == ["/bin/sh", "-c"] and "--collect-only" not in argv[2]:
            commands.append((argv[2], Path(kwargs["cwd"]), result[0], result[1]))
            finished(argv[2], Path(kwargs["cwd"]))
        return result

    monkeypatch.setattr(runner, "run_logged", measured)
    return commands


def install_owner_receipts(case, *, machine_accepts=True):
    """Real tests record execution only when the owner-specific env is enabled.

    CI has already measured the tip before that env is set. The machine test is
    outside acceptance/, so the existing CI transport cannot run it for us.
    """
    files = {}
    for n in (5, 1):
        path = f"tests/acceptance/test_own_{n}.py"
        files[path] = case.git.file_at(case.base, path) + (
            "\n    import os\n"
            "    if receipt := os.environ.get('FA_OWNER_CHECK_RECEIPT'):\n"
            "        with open(receipt, 'a') as stream:\n"
            f"            stream.write('locked:{n}\\n')\n"
        )
    files["tests/test_machine_requirement.py"] = (
        "from pathlib import Path\nimport os\nimport pytest\n"
        "@pytest.mark.local_only\ndef test_machine_requirement():\n"
        "    together = all(Path(f'src/member_{n}.py').exists() for n in (5, 1))\n"
        "    if receipt := os.environ.get('FA_OWNER_CHECK_RECEIPT'):\n"
        "        with open(receipt, 'a') as stream:\n"
        "            stream.write('machine:tip\\n' if together else 'machine:base\\n')\n"
        f"    assert {machine_accepts!r} or not together, 'machine requirement rejects composed tip'\n"
    )
    update_base(case, files)
    for n in (5, 1):
        approval = case.store.load_approval(n)
        blobs = case.git.blob_shas(case.base, approval.acceptance_blobs)
        case.store.save_approval(**replace(approval, acceptance_blobs=blobs).__dict__)
    return case.store.root / "owner-check-receipt.txt"


def assert_trusted_tip_ci(case, base, tip):
    assert case.git.blob_sha(tip, contract.WORKFLOW) == case.git.blob_sha(base, contract.WORKFLOW)
    artifacts = case.tracker.pr_acceptance_reports(tip)
    assert artifacts and all(row["run"]["head_sha"] == tip for row in artifacts)
    assert all(row["run"]["path"] == contract.WORKFLOW for row in artifacts)
    assert all(row["report"]["scope"]["marker"] == "not local_only" for row in artifacts)
    passing = {node for row in artifacts for node in row["report"]["head"]["passing_ids"]}
    assert {f"tests/acceptance/test_own_{n}.py::test_own" for n in (5, 1)} <= passing


def assert_owner_lock_released(case):
    lock = case.store.lock_acceptance_baseline("merge-owner", blocking=False)
    assert lock is not None, "The finished owner pass must release its independent lock"
    lock.close()


@pytest.mark.parametrize("change", ["target", "member-head"])
def test_invalidated_owner_stops_checks_and_next_tick_starts_a_new_pass(batched, monkeypatch, change):
    case = batched
    install_owner_receipts(case)
    case.config = replace(case.config, merge_batch_size=2)
    case.ctx.config = case.config
    base, tip = waiting_batch(case, [5, 1])
    green(case)
    invalidated = []

    def invalidate(command, root):
        case.clock[0] += timedelta(minutes=1)
        # This remains a real owner check if trusted CI stops the portable
        # Done when reruns: local_only work on the composed tip still executes.
        if invalidated or case.git.at(root).head_sha() != tip:
            return
        assert case.store.lock_acceptance_baseline("merge-owner", blocking=False) is None
        assert commands[-1][2:] == (0, False), "Invalidate after a genuine successful check"
        invalidated.append(len(commands))
        if change == "target":
            case.history.update({"src/neighbor.py": "VALUE = 'moved during owner work'\n"})
        else:
            support.update_pr_branch(case, 1)

    with monkeypatch.context() as patch:
        commands = observe_owner_commands(patch, finished=invalidate)
        support.gate(case)
    assert len(invalidated) == 1, "The invalidation must happen during real tip verification"
    retired = assert_unused(case, case.history.tip if change == "target" else base, tip)
    if change == "member-head":
        assert "member #1" in retired["reason"]
    else:
        assert case.history.tip not in {base, tip}
    assert_owner_lock_released(case)

    # A subsequent public tick must acquire the lock and publish unrelated
    # admissions, without reviving the retired batch.
    case.pending_batch = True
    for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(key, case.clock[0].isoformat())
    support.queue_workers(case, [4, 2])
    orch_queue.set_global(case.store, [4, 2, 5, 1])
    support.gate(case)
    assert len(groups(case)) == 2 and refs(case)
    next_tip = case.tracker.pr_head_sha(groups(case)[-1].number)
    assert next_tip != tip and case.ci.by_head[next_tip].present == {4, 2}
    assert len(events(case)) == len(batch.retirements(case.ctx)) == 1
    assert not case.target_attempts and not case.merges
    assert_owner_lock_released(case)
    assert not commands[invalidated[0]:], (
        "An invalidated owner kept executing checks before releasing its lock: "
        f"{[command for command, *_ in commands[invalidated[0]:]]}"
    )


def test_trusted_tip_ci_skips_locked_reruns_but_runs_machine_checks_once(batched, monkeypatch):
    case = batched
    receipt = install_owner_receipts(case)
    base, tip = waiting_batch(case, [5, 1])
    green(case)
    assert_trusted_tip_ci(case, base, tip)
    with monkeypatch.context() as patch:
        patch.setenv("FA_OWNER_CHECK_RECEIPT", str(receipt))
        commands = observe_owner_commands(patch)
        support.gate(case)
        support.gate(case)  # A completed admission must not recheck or land twice.
    assert case.target_attempts == case.target_pushes == [(tip, "refs/heads/main", base)]
    assert case.history.tip == tip and not case.merges and not refs(case)
    assert {n for n, _ in case.store.owner_merge_facts()} == {5, 1}
    assert all(case.tracker.get_issue(n).state == "closed" for n in (5, 1))
    event, = events(case)
    assert event["result"] == "used" and event["used_members"] == 2
    retired, = batch.retirements(case.ctx)
    assert retired["reason"] == "train merged"
    assert_owner_lock_released(case)
    executed = receipt.read_text().splitlines() if receipt.exists() else []
    assert executed == ["machine:tip"], (
        "Trusted tip CI must replace portable locked reruns and retain exactly one machine check; "
        f"executed {executed}, commands {[command for command, *_ in commands]}"
    )


def test_trusted_tip_ci_still_refuses_a_failing_local_only_check(batched, monkeypatch):
    case = batched
    receipt = install_owner_receipts(case, machine_accepts=False)
    base, tip = waiting_batch(case, [5, 1])
    green(case)
    assert_trusted_tip_ci(case, base, tip)
    with monkeypatch.context() as patch:
        patch.setenv("FA_OWNER_CHECK_RECEIPT", str(receipt))
        support.gate(case)
    retired = assert_unused(case, base, tip)
    assert "machine requirement rejects composed tip" in retired["reason"]
    assert receipt.read_text().splitlines().count("machine:tip") == 1
    assert all(case.tracker.get_issue(n).state == "open" for n in (5, 1))
    assert_owner_lock_released(case)


def test_one_composable_green_admission_lands_once_as_a_batch_of_one(batched):
    case = batched
    case.pending_batch = True
    support.queue_workers(case, [5])
    base = case.history.tip
    member_head = case.tracker.pr_head_sha(support.pr_for(case, 5).number)
    support.gate(case)
    assert len(groups(case)) == 1, "A lone composable admission must publish its own batch tip"
    tip = case.tracker.pr_head_sha(groups(case)[0].number)
    assert tip != member_head and case.ci.by_head[tip].present == {5}
    assert refs(case) and not events(case) and not batch.retirements(case.ctx)
    assert case.history.tip == base and not case.target_attempts and not case.merges
    green(case)
    support.gate(case)
    support.gate(case)
    assert case.target_attempts == case.target_pushes == [(tip, "refs/heads/main", base)]
    assert case.history.tip == tip and not case.merges and not refs(case)
    fact, = case.store.owner_merge_facts().values()
    assert fact["head"] == member_head and fact["pr"] == support.pr_for(case, 5).number
    assert case.tracker.get_issue(5).state == "closed"
    event, = events(case)
    assert event["result"] == "used" and event["used_members"] == 1
    retired, = batch.retirements(case.ctx)
    assert retired["reason"] == "train merged"
    assert len(case.batch_pushes) == 1
    assert_owner_lock_released(case)


def test_single_path_turn_rotates_after_refresh_while_another_admission_is_ready(batched, monkeypatch):
    case = batched
    # Both admissions genuinely require the single path, independently of
    # whether ordinary singleton batching is implemented.
    edit_member_workflows(case, monkeypatch, {
        5: contract.WORKFLOW, 1: ".github/workflows/foragent-acceptance-other.yaml",
    })
    support.queue_workers(case, [5])
    first_head = case.tracker.pr_head_sha(support.pr_for(case, 5).number)
    update_base(case, {"src/neighbor.py": "VALUE = 'requires a refreshed head'\n"})
    support.queue_workers(case, [1])  # This admission is already green on the current target.
    ready_head = case.tracker.pr_head_sha(support.pr_for(case, 1).number)
    orch_queue.set_global(case.store, [5, 1])
    first = support.gate(case)
    refreshed = case.tracker.pr_head_sha(support.pr_for(case, 5).number)
    assert refreshed != first_head and "#5 merge owner advanced" in first.actions
    assert case.tracker.pr_head_sha(support.pr_for(case, 1).number) == ready_head
    assert not case.merges and not case.target_pushes and not groups(case)
    assert_owner_lock_released(case)
    case.clock[0] += timedelta(minutes=1)
    second = support.gate(case)
    merged_on_second_pass = list(contract.turns(case))
    prs_on_second_pass = [row["pr"] for row in case.merges]
    assert_owner_lock_released(case)
    support.drain(case, count=2)
    support.gate(case)
    assert not case.target_pushes and not groups(case)
    assert len(case.merges) == len(case.store.owner_merge_facts()) == 2
    assert all(case.tracker.get_issue(n).state == "closed" for n in (5, 1))
    assert merged_on_second_pass == [1], (
        "After #5 used a turn to refresh, ready #1 must get the next turn; "
        f"landed {merged_on_second_pass}, actions {second.actions}"
    )
    assert prs_on_second_pass == [support.pr_for(case, 1).number]
    assert contract.turns(case) == [1, 5]
    assert "#1 merge owner advanced" in second.actions and "#5 merge owner advanced" not in second.actions


def test_single_path_pending_ci_yields_to_a_ready_admission(batched, monkeypatch):
    case = batched
    edit_member_workflows(case, monkeypatch, {
        5: contract.WORKFLOW, 1: ".github/workflows/foragent-acceptance-other.yaml",
    })
    support.queue_workers(case, [5, 1])
    orch_queue.set_global(case.store, [5, 1])
    case.tracker.set_pr_checks(support.pr_for(case, 5).number, "pending")
    report = support.gate(case)
    assert contract.turns(case) == [1] and "#1 merge owner advanced" in report.actions
    assert case.tracker.get_issue(5).state == "open"
    assert not case.target_pushes and not groups(case)
    assert_owner_lock_released(case)
    green(case)
    support.drain(case, count=1)
    support.gate(case)
    assert contract.turns(case) == [1, 5]
    assert len(case.merges) == len(case.store.owner_merge_facts()) == 2
    assert not case.target_pushes and not groups(case)


def assert_checked_batch_lands_once(case, monkeypatch, base, tip):
    commands = observe_owner_commands(monkeypatch)
    support.gate(case)
    assert case.target_attempts == case.target_pushes == [(tip, "refs/heads/main", base)], (
        f"Passing owner checks must land the batch: {batch.retirements(case.ctx)}; "
        f"check results: {[(command, code, timeout) for command, _, code, timeout in commands]}"
    )
    for n in (5, 1):
        approved, = parse_body(case.store.load_approval(n).body).done_when_commands
        checked, = [(code, timeout) for command, _, code, timeout in commands if approved in command]
        assert checked == (0, False), f"Member #{n}'s Done when must pass: {commands}"
    assert case.history.tip == tip and not case.merges and not refs(case)
    assert {n for n, _ in case.store.owner_merge_facts()} == {5, 1}
    assert all(case.tracker.get_issue(n).state == "closed" for n in (5, 1))
    event, = events(case)
    assert event["result"] == "used" and event["used_members"] == 2
    retired, = batch.retirements(case.ctx)
    assert retired["reason"] == "train merged"
    assert_owner_lock_released(case)
    checked = list(commands)
    support.gate(case)
    assert case.target_attempts == case.target_pushes == [(tip, "refs/heads/main", base)]
    assert commands == checked and len(case.store.owner_merge_facts()) == 2
    assert len(events(case)) == len(batch.retirements(case.ctx)) == 1


def test_green_batch_owner_checks_land_once_with_space_in_state_path(batched, monkeypatch):
    case = batched
    root = case.store.root.with_name("owner state")
    case.store.root.rename(root)
    case.store = StateStore(root, now=lambda: case.clock[0])
    case.ctx.store = case.store
    base, tip = waiting_batch(case, [5, 1])
    green(case)
    assert " " in str(case.store.root)
    assert_trusted_tip_ci(case, base, tip)
    assert_checked_batch_lands_once(case, monkeypatch, base, tip)


def chained_done_when_batch(case, monkeypatch, *, failing_b=False):
    """A stays locked; B is a unit file with real, exact-tip portable CI evidence."""
    path = "tests/test_chain_b.py"
    contents = "def test_ci_passed():\n    pass\n"
    if failing_b:
        contents += (
            "\nimport pytest\nfrom pathlib import Path\n"
            "@pytest.mark.local_only\ndef test_composed_tip():\n"
            "    assert not all(Path(f'src/member_{n}.py').exists() for n in (5, 1)), "
            "'B rejects the composed tip'\n"
        )
    update_base(case, {path: contents})
    approval = case.store.load_approval(5)
    first, = parse_body(approval.body).done_when_commands
    # Keep B's node ids relative to the small repo even when TMPDIR is inside
    # the checkout and pytest could otherwise discover Foragent's own config.
    second = f"{contract.PYTHON} -m pytest -q -p no:cacheprovider --rootdir=. {path}"
    body = approval.body.replace(first, f"{first} && {second}")
    case.tracker.update_issue(5, title=case.tracker.get_issue(5).title, body=body)
    case.store.save_approval(**replace(approval, body=body, body_sha=body_hash(body)).__dict__)
    base, tip = waiting_batch(case, [5, 1])
    green(case)
    assert_trusted_tip_ci(case, base, tip)

    # Extend only the offline CI transport: actually run B's portable tests on
    # the immutable published tip, then serve its passing ids for that SHA alone.
    directory = case.scratch / "unit-tip"
    contract.archive(case, tip, directory)
    report = directory / "unit.xml"
    measured = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir=.",
         "-o", "addopts=", "-o", "junit_family=legacy", "-m", "not local_only",
         f"--junitxml={report}", path],
        cwd=directory, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTEST_ADDOPTS": "", "PYTHONPATH": ""},
    )
    assert measured.returncode == 0, measured.stdout + measured.stderr
    cases = list(ET.parse(report).iter("testcase"))
    assert cases and all(not any(node.find(tag) is not None for tag in ("failure", "error", "skipped"))
                         for node in cases)
    passing = {f"{node.get('file')}::{node.get('name')}" for node in cases}
    assert passing == {f"{path}::test_ci_passed"}
    lookups = []

    def receipt(head):
        lookups.append(head)
        return (case.ci.by_head[tip].run_id, set(passing)) if head == tip else None

    monkeypatch.setattr(case.tracker, "pr_unit_passed_tests", receipt)
    return base, tip, lookups


def test_green_batch_lands_once_when_chained_b_was_all_passed_by_tip_ci(batched, monkeypatch):
    case = batched
    base, tip, lookups = chained_done_when_batch(case, monkeypatch)
    assert_checked_batch_lands_once(case, monkeypatch, base, tip)
    assert lookups and set(lookups) == {tip}


def test_chained_b_real_failure_still_retires_green_batch(batched, monkeypatch):
    case = batched
    base, tip, lookups = chained_done_when_batch(case, monkeypatch, failing_b=True)
    commands = observe_owner_commands(monkeypatch)
    support.gate(case)
    retired = assert_unused(case, base, tip)
    assert retired["reason"].startswith("member #5"), retired["reason"]
    assert "checks-failed" in retired["reason"]
    chained = [(code, timeout) for command, _, code, timeout in commands
               if "&&" in command and "tests/test_chain_b.py" in command]
    assert chained == [(1, False)], commands
    assert "B rejects the composed tip" in case.store.log_path(5).read_text()
    assert lookups and set(lookups) == {tip}
    assert all(case.tracker.get_issue(n).state == "open" for n in (5, 1))
    assert_owner_lock_released(case)
