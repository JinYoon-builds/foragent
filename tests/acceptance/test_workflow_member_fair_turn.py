"""#2893 r1308: a workflow-changing member takes a fair turn in ready order.

Two review holds of one class: the workflow member's single turn either
starved behind trains when main moved, or stopped every batch while its
refreshed CI was pending. The rule: ready members reach main in the order they
became ready; a workflow-changing member takes its own single landing in that
order; a member whose checks are pending never stops others, and it keeps its
place once its checks pass.

World: the shared merge-owner batch world (fake tracker, local Git with a bare
origin, measured tip CI, public scheduler tick). No network.

How landing order is read: members other than #5 never change the acceptance
workflow file. So a landed batch tip that already carries #5's changed
workflow landed after #5; a tip that still carries the old workflow landed
before #5.

CI model: the fake GitHub reports a member's checks as passed at once unless a
test holds them. The statement-3 test holds #5's checks "pending" from its
first refreshed head until the test releases them; after release, every #5 head
passes at once (fast CI).
"""

import pytest

from workflow_alone_world import (
    CHANGED, WORKFLOW, batches, head, merge_facts, merged, record_checks, support, waiting,
)
from conftest import git_command
from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
from helpers.merge_batch import batched, edit_member_workflows, groups, queue_workers  # noqa: F401
from foragent import merge_owner_batch as batch


@pytest.fixture
def stream(request, monkeypatch):
    from helpers import merge_world as contract

    monkeypatch.setattr(contract, "NUMBERS", (5, 1, 4, 2, 6, 3, 7))
    return request.getfixturevalue("batched")


def landed_tips(case):
    """(tip sha, members) of every batch whose tip reached main, in landing order."""
    pushed = [sha for sha, *_ in case.target_pushes]
    found = []
    for pr in groups(case):
        tip = case.tracker.pr_head_sha(pr.number)
        if tip in pushed:
            _, chain = batch._decode(case.git, pr.head, tip)
            found.append((pushed.index(tip), tip, sorted(n for n, *_ in chain)))
    return [(tip, members) for _, tip, members in sorted(found)]


def carries_changed_workflow(case, sha):
    try:
        return git_command(case.history.origin, "show", f"{sha}:{WORKFLOW}").strip() == CHANGED.strip()
    except Exception:  # noqa: BLE001 - the file may be absent on the old side
        return False


def passed_over(case, later):
    """Landed batches holding a member in `later` that reached main before #5."""
    return [members for tip, members in landed_tips(case)
            if set(members) & set(later) and not carries_changed_workflow(case, tip)]


def assert_merged_once_on_its_own(case, changed):
    assert merged(case, 5), "#5 changes the acceptance workflow and never merged"
    assert all(5 not in b for b in batches(case)), f"#5 must not join a batch: {batches(case)}"
    facts = merge_facts(case, 5)
    assert len(facts) == 1 and case.git.is_ancestor(changed, facts[0]["head"]), (changed, facts)
    assert carries_changed_workflow(case, git_command(case.history.origin, "rev-parse", "main"))
    assert [row["pr"] for row in case.merges].count(support.pr_for(case, 5).number) <= 1, case.merges
    assert case.tracker.get_issue(5).state == "closed"


def assert_ordinary_members_batched(case, ordinary):
    in_batches = sorted(n for _, members in landed_tips(case) for n in members)
    assert in_batches == sorted(ordinary), (landed_tips(case), ordinary)
    assert all(len(merge_facts(case, n)) == 1 for n in ordinary)
    assert not waiting(case)


def run_stream(case, waves, extra=8):
    """One owner pass per wave; each wave becomes ready after that pass. Then drain."""
    actions = []
    for wave in waves:
        actions += support.gate(case).actions
        queue_workers(case, wave)
    for _ in range(extra):
        actions += support.gate(case).actions
        if not waiting(case):
            break
    return actions


def test_main_moved_after_workflow_member_ready_it_lands_before_later_ready_members(stream, monkeypatch):
    """Main moves (outside commit) after #5 is ready; #1 and #4 become ready after that."""
    case = stream
    edit_member_workflows(case, monkeypatch, {5: WORKFLOW})
    queue_workers(case, [5])
    changed = head(case, 5)
    case.history.update({"src/neighbor.py": "VALUE = 'main moved'\n"})  # main moves after #5 is ready
    queue_workers(case, [1, 4])  # ready after #5
    measured = record_checks(monkeypatch)

    actions = run_stream(case, [[2, 6], [3, 7]])

    later = [1, 4, 2, 6, 3, 7]
    assert not passed_over(case, later), (
        f"batches of members that became ready after #5 reached main before #5: {passed_over(case, later)}; "
        f"landed={[m for _, m in landed_tips(case)]}; actions={actions}")
    assert_merged_once_on_its_own(case, changed)
    assert 5 in set(measured), f"#5's own merge-owner checks did not run: {measured}"
    assert_ordinary_members_batched(case, later)


def test_train_of_earlier_member_moves_main_and_later_trains_do_not_pass_workflow_member(stream, monkeypatch):
    """#1 is ready before #5, #4 and #2 after it. A train of #1 may move main first; no later train may."""
    case = stream
    edit_member_workflows(case, monkeypatch, {5: WORKFLOW})
    queue_workers(case, [1, 5, 4, 2])  # ready order: 1, 5, 4, 2 (one second apart)
    changed = head(case, 5)
    record_checks(monkeypatch)

    actions = run_stream(case, [[6], [3], [7]])

    later = [4, 2, 6, 3, 7]
    assert not passed_over(case, later), (
        f"trains of members that became ready after #5 kept passing it: {passed_over(case, later)}; "
        f"landed={[m for _, m in landed_tips(case)]}; actions={actions}")
    assert_merged_once_on_its_own(case, changed)
    assert_ordinary_members_batched(case, [1, *later])


def test_pending_refreshed_checks_do_not_block_batches_and_member_keeps_its_place(stream, monkeypatch):
    case = stream
    edit_member_workflows(case, monkeypatch, {5: WORKFLOW})
    queue_workers(case, [5])
    changed = head(case, 5)
    case.history.update({"src/neighbor.py": "VALUE = 'main moved'\n"})  # #5 must refresh onto main
    queue_workers(case, [1, 4])
    record_checks(monkeypatch)
    pr5 = support.pr_for(case, 5).number
    ci = {"held": True, "heads": []}
    set_head = case.tracker.set_pr_head_sha

    def published(pr, sha, *args, **kwargs):
        result = set_head(pr, sha, *args, **kwargs)
        if pr == pr5:
            ci["heads"].append(sha)
            case.tracker.set_pr_checks(pr5, "pending" if ci["held"] else "pass")
        return result

    monkeypatch.setattr(case.tracker, "set_pr_head_sha", published)
    actions = []

    # Owner passes until #5 has a refreshed head; its checks are then pending.
    for _ in range(4):
        actions += support.gate(case).actions
        if ci["heads"]:
            break
    assert ci["heads"], f"#5 was never refreshed onto the moved main: {actions}"
    assert case.tracker.pr_checks(pr5) == "pending"

    # While #5's refreshed checks are pending, other ready members keep landing in batches.
    queue_workers(case, [2, 6])
    for _ in range(4):
        actions += support.gate(case).actions
        if {1, 4, 2, 6} <= {n for _, m in landed_tips(case) for n in m}:
            break
    landed = [m for _, m in landed_tips(case)]
    assert {1, 4, 2, 6} <= {n for m in landed for n in m}, (
        f"a pending workflow member stopped other ready members from landing: landed={landed}; "
        f"waiting={waiting(case)}; actions={actions}")
    assert not merged(case, 5), "#5 merged while its refreshed checks were still pending"

    # Its checks pass; members that become ready now must not land before it.
    ci["held"] = False
    case.tracker.set_pr_checks(pr5, "pass")
    queue_workers(case, [3, 7])
    for _ in range(8):
        actions += support.gate(case).actions
        if not waiting(case):
            break

    assert not passed_over(case, [3, 7]), (
        f"#5 lost its place after its checks passed: {passed_over(case, [3, 7])} landed first; "
        f"landed={[m for _, m in landed_tips(case)]}; actions={actions}")
    assert_merged_once_on_its_own(case, changed)
    assert_ordinary_members_batched(case, [1, 4, 2, 6, 3, 7])
