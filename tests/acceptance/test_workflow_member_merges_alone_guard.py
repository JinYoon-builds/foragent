"""Guards for PM r1280 (not in Done when: these already pass on main and must keep passing).

The PM answer (11-09) moved the lone-member case out of the behaviour
statements: it already works in the merge-owner world and stays a guard here.

- The only ready member changes the acceptance workflow and its merge-owner
  checks pass: the merge owner merges it on its own, outside any batch.
- Members that do not change the workflow still batch together.
"""

from workflow_alone_world import (
    CHANGED, WORKFLOW, batches, head, landed_batches, merge_facts, record_checks, support, waiting,
)
from conftest import git_command
from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
from helpers.merge_batch import batched, edit_member_workflows, queue_workers  # noqa: F401


def test_only_ready_member_changing_the_workflow_merges_on_its_own(batched, monkeypatch):
    case = batched
    edit_member_workflows(case, monkeypatch, {5: WORKFLOW})
    queue_workers(case, [5])
    changed = head(case, 5)
    case.history.update({"src/neighbor.py": "VALUE = 'main moved'\n"})  # its checks must run on the new main
    record_checks(monkeypatch)
    actions = []
    for _ in range(9):
        actions += support.gate(case).actions
        if not waiting(case):
            break
    assert not waiting(case), actions
    assert not batches(case), f"a workflow-changing member must not be put into a batch: {batches(case)}"
    facts = merge_facts(case, 5)
    assert len(facts) == 1 and case.git.is_ancestor(changed, facts[0]["head"]), (changed, facts)
    assert git_command(case.history.origin, "show", f"{facts[0]['head']}:{WORKFLOW}") == CHANGED
    assert [row["pr"] for row in case.merges] == [support.pr_for(case, 5).number], case.merges
    assert case.tracker.get_issue(5).state == "closed"


def test_members_that_do_not_change_the_workflow_still_batch_together(batched):
    case = batched
    queue_workers(case, [5, 1, 4])
    support.gate(case)
    assert [sorted(b) for b in landed_batches(case)] == [[1, 4, 5]], (batches(case), case.target_pushes)
    assert len(case.target_pushes) == 1 and not case.merges
    assert all(len(merge_facts(case, n)) == 1 for n in [5, 1, 4])
    assert not waiting(case)
