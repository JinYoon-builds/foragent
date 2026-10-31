"""Locked acceptance for group-recovery. Do not edit during implementation.

Break-it: dropping the whole failed group, always blaming its leader, merging
a culprit, writing another owner's state, waiting forever for a dead member,
or relying on a missing last member for cleanup fails these runner scenarios.
No exact search algorithm, partition sizes, counters or event shape is locked.
"""

import pytest

from conftest import git_command
from test_merge_group import (  # noqa: F401
    RunnerGone, contend, group_case, isolated_home, offline_acceptance, prepare, publish_case,
)


def assert_ownership(case):
    for row in case.merges:
        pr = next(pr for pr in case.tracker.created_prs if pr.number == row.pr)
        assert row.issue == int(pr.head.rsplit("-", 1)[1]), "Only an owner merges its PR"
    assert all(actor == n for actor, n in case.closes)
    assert all(actor == n for actor, n in case.record_writes)
    assert all(actor == n for actor, n, _ in case.board_writes)
    assert all(actor == row["n"] for actor, row in case.appends
               if row["event"] in {"ready", "withdrawn", "turn", "merged"})


@pytest.mark.parametrize("culprits", [(5,), (6,), (5, 6)], ids=["leader", "tail", "two-culprits"])
def test_failed_joint_check_isolates_culprits_and_keeps_survivors_moving(group_case, culprits):
    case = group_case
    prepare(case, (5, 1, 4, 6), culprits=culprits)
    contend(case)
    joint = [row for row in case.probes if row.armed and row.before_merges == 0
             and row.present == frozenset(case.members) and "tools/group_probe.py" in row.command]
    assert joint, "Culprit isolation must start from the failed combined check"
    assert any(row.result.exit_code != 0 for row in joint)
    survivors = [n for n in case.members if n not in culprits]
    assert [row.issue for row in case.merges] == survivors, "Skip culprits while preserving the surviving FIFO turns"
    for n in survivors:
        assert case.outcomes[n] == "merged"
        assert case.tracker.get_issue(n).state == "closed"
    final_files = git_command(case.history.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    for n in culprits:
        assert f"src/member_{n}.py" not in final_files, "A culprit must never reach main"
        assert case.outcomes[n] == "retries-exceeded"
        assert case.tracker.get_issue(n).state == "open"
        assert f"base-move culprit: {n}" in case.store.log_path(n).read_text()
        assert any(row.armed and n in row.present and len(row.present) < len(case.members)
                   and row.result.exit_code != 0 for row in case.probes), (
            "Establish narrower failure evidence instead of blaming the whole group"
        )
    assert len(case.implementer.calls) == len(case.reviewer.calls) == len(case.members)
    assert_ownership(case)


def test_failed_joint_evidence_cannot_be_discarded_as_a_neutral_docs_move(group_case):
    case = group_case
    prepare(case, (1, 4), culprits=(1,))
    case.move_on_ready = False
    probe = git_command(case.history.origin, "show", "main:tools/group_probe.py") + "\n"
    probe = probe.replace("member['CULPRIT'] and epoch",
                          "member['CULPRIT'] and Path('docs/group-trigger.md').exists()")
    case.history.update({"tools/group_probe.py": probe})
    git_command(case.root, "fetch", "-q", "origin")
    git_command(case.root, "merge", "-q", "--ff-only", "origin/main")
    moved = []

    def green(pr, head, poll):
        if case.worker.number == 4 and not moved:
            with case.external():
                case.history.update({"docs/group-trigger.md": "New documented behavior\n"})
                moved.append(case.history.tip)
        return "pass"

    case.ci_status = green
    contend(case)
    joint = [row for row in case.probes if row.armed and row.before_merges == 0
             and row.present == frozenset((1, 4)) and "tools/group_probe.py" in row.command]
    assert joint and any(row.result.exit_code != 0 for row in joint), "Exercise a failing joint check on a docs-only move"
    assert len(moved) == 1
    assert [row.issue for row in case.merges] == [4], "Known failed evidence must override neutral-path reuse"
    assert case.outcomes[1] == "retries-exceeded"
    assert case.outcomes[4] == "merged"
    assert case.tracker.get_issue(1).state == "open"
    assert "base-move culprit: 1" in case.store.log_path(1).read_text()
    assert_ownership(case)


@pytest.mark.parametrize("departed", [4, 5], ids=["middle", "last"])
def test_dead_member_is_skipped_and_successful_merges_are_finalized(group_case, departed):
    case = group_case
    prepare(case, (1, 4, 5))

    def disappear(n):
        if n == 1:
            with case.external():
                case.store.finish_run(departed, outcome="crashed")
            case.dead.add(departed)
            case.wake()

    case.on_merged = disappear
    contend(case)
    assert any(row.armed and row.before_merges == 0 and len(row.present) == 3
               for row in case.probes), "Death recovery must exercise an actual checked group"
    survivors = [n for n in case.members if n != departed]
    assert [row.issue for row in case.merges] == survivors
    assert all(case.outcomes[n] == "merged" for n in survivors)
    assert case.tracker.get_issue(departed).state == "open"
    assert case.store.get_run(departed).outcome == "crashed"
    assert any(row.armed and row.present == frozenset(survivors)
               and row.result.exit_code == 0 and not row.result.timed_out for row in case.probes), (
        "The surviving published contents need their own passing evidence when a checked member drops out"
    )
    assert len(case.reinstalls) == 1 and case.reinstalls[0][2] == len(survivors), (
        "Finalize the successful group once even when the planned final member disappears"
    )
    assert_ownership(case)


def test_changed_last_head_cannot_lose_finalization_for_prior_merges(group_case):
    case = group_case
    prepare(case, (1, 4, 5))

    def replace_last(n):
        if n == 1:
            with case.external():
                case.tracker.set_pr_head_sha(case.tracker.find_pr("foragent/issue-5"), "f" * 40)

    case.on_merged = replace_last
    contend(case)
    assert any(row.armed and row.before_merges == 0 and len(row.present) == 3
               for row in case.probes), "Finalization must cover an actual checked group"
    assert [row.issue for row in case.merges] == [1, 4]
    assert all(case.outcomes[n] == "merged" for n in (1, 4))
    assert case.outcomes[5] == "held" and case.tracker.get_issue(5).state == "open"
    assert any(row.armed and row.present == frozenset((1, 4))
               and row.result.exit_code == 0 and not row.result.timed_out for row in case.probes)
    assert len(case.reinstalls) == 1 and case.reinstalls[0][2] == 2, (
        "A last member rejected before consuming its group result must not lose after-merge work"
    )
    assert_ownership(case)


def test_dead_leader_with_no_result_does_not_strand_the_remaining_turns(group_case):
    case = group_case
    prepare(case, (1, 4, 5))
    interrupted = []

    def disappear_before_result(_row):
        if not interrupted:
            interrupted.append(True)
            with case.external():
                case.store.finish_run(1, outcome="crashed")
            raise RunnerGone()

    # This existing fake delivers process death after group admission. The
    # assertions below inspect only progress and owned effects, not the claim.
    case.after_claim = disappear_before_result
    contend(case)
    assert interrupted, "Missing group admission: the leader-loss scenario was never exercised"
    assert [row.issue for row in case.merges] == [4, 5]
    assert all(case.outcomes[n] == "merged" for n in (4, 5))
    assert case.store.get_run(1).outcome == "crashed"
    assert case.tracker.get_issue(1).state == "open"
    assert len(case.reinstalls) == 1 and case.reinstalls[0][2] == 2
    assert_ownership(case)
