"""PM r1280: a ready member whose PR changes the acceptance workflow merges on its own.

#2768 (PR #2850) changed .github/workflows/foragent-acceptance.yml. The merge
owner excluded it from every train ("merge train excluded: acceptance workflow
differs from target") while other members kept forming trains that landed on
main. It got a single-path turn only when no train formed, and each landed
train moved main under its checks, so it waited 80+ minutes until the PM merged
it by hand.

The world below is that stream: the workflow-changing member waits together
with other ready members, and new ordinary members keep arriving after each
owner pass. The other members must still merge in a batch, and the
workflow-changing member must merge on its own before or after that batch,
not after every later train too.

Choice: "before or after that batch" is checked as "no later than the train of
the second wave of later arrivals". One later train may land in between (an
implementation may need a pass for the member's own checks); a stream may not
hold it back for good.
"""

import pytest

from workflow_alone_world import (
    CHANGED, WORKFLOW, batches, head, landed_batches, merge_facts, merged, record_checks, support, waiting,
)
from conftest import git_command
from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
from helpers.merge_batch import batched, edit_member_workflows, queue_workers  # noqa: F401

FIRST = [1, 4]  # ordinary members that wait together with #5
WAVES = [[2, 6], [3, 7]]  # ordinary members that arrive after each owner pass


@pytest.fixture
def stream(request, monkeypatch):
    from helpers import merge_world as contract

    monkeypatch.setattr(contract, "NUMBERS", (5, 1, 4, 2, 6, 3, 7))
    return request.getfixturevalue("batched")


def _first_pass(history, predicate):
    return next((i for i, row in enumerate(history) if predicate(row)), None)


def test_workflow_member_merges_alone_while_other_members_keep_batching(stream, monkeypatch):
    case = stream
    edit_member_workflows(case, monkeypatch, {5: WORKFLOW})
    queue_workers(case, [5, *FIRST])
    changed = head(case, 5)
    measured = record_checks(monkeypatch)
    history, actions = [], []
    for wave in [*WAVES, None, None, None, None, None, None]:
        actions += support.gate(case).actions
        history.append({"merged5": merged(case, 5), "landed": [sorted(b) for b in landed_batches(case)]})
        if wave is not None:
            queue_workers(case, wave)
        elif not waiting(case):
            break

    # The other members that waited with #5 still merged together in a batch, without #5.
    assert sorted(FIRST) in [sorted(b) for b in landed_batches(case)], (landed_batches(case), actions)
    assert all(5 not in b for b in batches(case)), f"#5 must not join a batch: {batches(case)}"

    # #5 merged on its own, once, and is not held behind every later train.
    last_wave = set(WAVES[-1])
    merged_at = _first_pass(history, lambda row: row["merged5"])
    last_train_at = _first_pass(history, lambda row: any(set(b) & last_wave for b in row["landed"]))
    assert merged_at is not None, (
        "#5 changes the acceptance workflow and was never merged while other members kept batching; "
        f"history={history}; actions={actions}")
    assert last_train_at is None or merged_at <= last_train_at, (
        f"#5 waited behind every later train: merged at pass {merged_at}, "
        f"the train of {sorted(last_wave)} landed at pass {last_train_at}; history={history}")
    facts = merge_facts(case, 5)
    assert len(facts) == 1 and case.git.is_ancestor(changed, facts[0]["head"]), (changed, facts)
    assert git_command(case.history.origin, "show", f"{facts[0]['head']}:{WORKFLOW}") == CHANGED
    assert [row["pr"] for row in case.merges].count(support.pr_for(case, 5).number) <= 1, case.merges
    assert case.tracker.get_issue(5).state == "closed"
    assert 5 in set(measured), f"#5's own merge-owner checks did not run: {measured}"

    # Every ordinary member still reached main through a batch; none was dropped or merged twice.
    ordinary = [*FIRST, *(n for wave in WAVES for n in wave)]
    in_batches = [n for b in landed_batches(case) for n in b]
    assert sorted(in_batches) == sorted(ordinary), (landed_batches(case), actions)
    assert all(len(merge_facts(case, n)) == 1 for n in ordinary)
    assert not waiting(case), actions


def test_workflow_member_is_not_passed_over_by_later_single_member_trains(stream, monkeypatch):
    """One ordinary member arrives after each pass; each forms a batch of one and lands at once."""
    case = stream
    edit_member_workflows(case, monkeypatch, {5: WORKFLOW})
    queue_workers(case, [5, 1])
    changed = head(case, 5)
    record_checks(monkeypatch)
    history = []
    for arrival in [4, 2, 6, 3, 7]:
        support.gate(case)
        history.append((merged(case, 5), [sorted(b) for b in landed_batches(case)]))
        queue_workers(case, [arrival])
    support.gate(case)
    history.append((merged(case, 5), [sorted(b) for b in landed_batches(case)]))

    assert [1] in [sorted(b) for b in landed_batches(case)], history
    assert all(5 not in b for b in batches(case)), batches(case)
    # #5 waited with #1. One later train may land before it (as in the test above), not every one.
    assert any(done for done, _ in history[:3]), (
        f"#5 stayed unmerged while single-member trains kept landing: {history}")
    for _ in range(6):
        if not waiting(case):
            break
        support.gate(case)
    assert not waiting(case)
    assert [sorted(b) for b in landed_batches(case)] and all(len(merge_facts(case, n)) == 1 for n in [1, 4, 2, 6, 3, 7])
    facts = merge_facts(case, 5)
    assert len(facts) == 1 and case.git.is_ancestor(changed, facts[0]["head"]), (changed, facts)
    assert git_command(case.history.origin, "show", f"{facts[0]['head']}:{WORKFLOW}") == CHANGED
