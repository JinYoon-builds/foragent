"""Acceptance: a merge batch is judged with the scope fixed when it was built.

Incident 2026-11-05: batch #2095 (6 members) had green CI, main did not move,
and it was still retired with "prefix measurement scope no longer covers the
current one", because the owner recomputed the acceptance exclusions at
landing time from the moving open-issue set.

These tests drive the public scheduler tick and the real merge owner
(tests/test_merge_owner_proto.gate) over the real Git, state and measured CI
transport of the locked merge-path world. Only GitHub, models and the clock
are fake. "Unrelated" means an issue that is not a batch member.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from conftest import git_command  # noqa: E402
from acceptance import test_merge_path_real_settings as contract  # noqa: E402
from acceptance.test_merge_path_real_settings import world  # noqa: E402, F401
from test_merge_owner_proto import proto  # noqa: E402, F401
import test_merge_owner_proto as support  # noqa: E402
from test_merge_owner_batch import (  # noqa: E402, F401
    batched, events, green, groups, queue_workers, refs, update_base,
)
from foragent import merge_owner_batch as batch  # noqa: E402
from foragent.issue import body_hash  # noqa: E402
from foragent.run_baseline import pending_acceptance_exclusions  # noqa: E402
from foragent.state import StateStore  # noqa: E402

CLOSING = contract.CLOSING_LOCK          # locked by unrelated open issue #77
OUTSIDER = 4                             # approved, unrelated, test_own_4.py locked
OUTSIDER_LOCK = f"tests/acceptance/test_own_{OUTSIDER}.py"
NEW_ISSUE = 81                           # opened while the batch waits
SCOPE_WORDS = "scope no longer covers"


# ---------------------------------------------------------------- helpers

def build_waiting_batch(case, members):
    """Build and publish a batch whose CI has not reported yet."""
    case.pending_batch = True
    queue_workers(case, members)
    support.gate(case)
    assert len(groups(case)) == 1 and refs(case), "the batch was not built"
    assert not case.target_pushes and not events(case), "the batch must still wait"
    pr = groups(case)[0]
    tip = case.tracker.pr_head_sha(pr.number)
    base, chain = batch._decode(case.git, pr.head, tip)
    assert sorted(n for n, *_ in chain) == sorted(members)
    return base, tip


def current_exclusions(case, base, tip, members):
    """The exclusion list a recomputation from today's open-issue set gives."""
    return pending_acceptance_exclusions(case.ctx, (base, tip), case.store.log_path(members[0]),
                                         own=members)


def close_unrelated(case, n=77):
    """An unrelated issue's work merges elsewhere and the issue closes."""
    case.store.start_run(n, branch=f"closing-{n}", worktree=case.store.root / f"closing-{n}")
    case.store.finish_run(n, outcome="merged")
    case.tracker.close_issue(n)


def open_unrelated(case, n=NEW_ISSUE):
    """A new unrelated issue is opened and approved; it locks an existing test."""
    body = ("<!-- foragent:v1 -->\n## Goal\nUnrelated new work.\n## Out of scope\nThe batch.\n"
            f"## Files\n- src/other_{n}.py\n## Done when\n```sh\n"
            f"{contract.PYTHON} -m pytest -q {contract.CONTRACT}\n```\n")
    case.tracker.add_issue(n, f"Unrelated {n}", body)
    case.tracker.record_label(n, "approved", "human")
    case.store.save_approval(n, body=body, body_sha=body_hash(body),
                             acceptance_blobs=case.git.blob_shas("origin/main", [contract.CONTRACT]),
                             approved_by="human", approved_at=case.clock[0].isoformat())


def relabel_unrelated(case, n=OUTSIDER):
    """An unrelated issue's approval is invalidated: labels change, approval is cleared."""
    from foragent.actions import invalidate_approval

    invalidate_approval(case.ctx, n, "Issue body changed after approval")
    assert "approved" not in case.tracker.get_issue(n).labels


def scope_retirements(case):
    return [row for row in batch.retirements(case.ctx) if SCOPE_WORDS in row.get("reason", "")]


def assert_landed_once(case, base, tip, members):
    assert not scope_retirements(case), batch.retirements(case.ctx)
    assert case.target_pushes == [(tip, "refs/heads/main", base)], (
        "the waiting batch must land with one target update", batch.retirements(case.ctx))
    assert case.history.tip == tip and not case.merges and not refs(case)
    assert len(case.store.owner_merge_facts()) == len(members)
    assert all(case.tracker.get_issue(n).state == "closed" for n in members)
    record, = events(case)
    assert record["result"] == "used" and record["used_members"] == len(members)
    support.gate(case)  # a replayed tick never lands the batch a second time
    assert len(case.target_pushes) == 1 and len(case.store.owner_merge_facts()) == len(members)


def assert_not_landed(case, base):
    assert not case.target_pushes and not case.merges and case.history.tip == base
    assert not case.store.owner_merge_facts()


# ------------------------------------------------- 1. unrelated issue changes

@pytest.mark.parametrize("change", ["close", "open", "relabel"])
def test_unrelated_issue_change_while_batch_waits_does_not_retire_it(batched, change):
    case = batched
    base, tip = build_waiting_batch(case, [5, 1])
    built = current_exclusions(case, base, tip, [5, 1])
    assert CLOSING in built and OUTSIDER_LOCK in built and contract.CONTRACT not in built
    {"close": close_unrelated, "open": open_unrelated, "relabel": relabel_unrelated}[change](case)
    # The moving open-issue set now gives another list than the one CI measured with.
    assert current_exclusions(case, base, tip, [5, 1]) != built
    case.clock[0] += timedelta(minutes=18)  # owner checks take time, main does not move
    green(case)
    support.gate(case)
    assert_landed_once(case, base, tip, [5, 1])


# ------------------------------------- 2. judged against the measured exclusions

def test_batch_lands_with_build_time_reports_after_owner_restart(batched):
    case = batched
    base, tip = build_waiting_batch(case, [5, 1])
    built = current_exclusions(case, base, tip, [5, 1])
    close_unrelated(case)
    green(case)
    reports = case.tracker.pr_acceptance_reports(tip)
    assert reports and all(row["report"]["excluded"] == built for row in reports)
    support.gate(case)
    assert_landed_once(case, base, tip, [5, 1])


def test_reports_measured_with_another_exclusion_list_do_not_land(batched, monkeypatch):
    """CI evidence that matches today's recomputed list but not the batch's list is refused.

    A new unrelated issue locks CONTRACT while the batch waits. Reports that
    claim CONTRACT was excluded were not measured with the batch's list, so
    they skip a test the batch still has to keep passing.
    """
    case = batched
    base, tip = build_waiting_batch(case, [5, 1])
    built = current_exclusions(case, base, tip, [5, 1])
    open_unrelated(case)
    today = current_exclusions(case, base, tip, [5, 1])
    assert contract.CONTRACT not in built and contract.CONTRACT in today
    reports = case.tracker.pr_acceptance_reports

    def measured_with_todays_list(head):
        values = reports(head)
        if head == tip:
            for value in values:
                value["report"]["excluded"] = sorted({*value["report"]["excluded"], contract.CONTRACT})
                value["report"]["scope"]["excluded"] = list(value["report"]["excluded"])
                for side in ("base", "head"):
                    for key in ("collected_ids", "passing_ids"):
                        value["report"][side][key] = [node for node in value["report"][side][key]
                                                      if not node.startswith(contract.CONTRACT + "::")]
        return values

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", measured_with_todays_list)
    green(case)
    support.gate(case)
    support.gate(case)
    # The members may still merge one by one on their own evidence; the batch
    # tip judged with the wrong list must never become main.
    assert not case.target_pushes, batch.retirements(case.ctx)
    assert not case.git.at(case.history.origin).is_ancestor(tip, "refs/heads/main")


# ------------------------------------------------------------- 3. safety stays

def test_red_tip_still_stops_the_batch_after_an_unrelated_close(batched, monkeypatch):
    case = batched
    base, tip = build_waiting_batch(case, [5, 1])
    close_unrelated(case)
    reports = case.tracker.pr_acceptance_reports

    def regression(head):
        values = reports(head)
        if head == tip:
            for value in values:
                value["report"]["head"]["passing_ids"] = []
        return values

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", regression)
    green(case)
    support.gate(case)
    assert_not_landed(case, base)
    assert not refs(case)
    retirement, = batch.retirements(case.ctx)
    assert retirement["reason"] == "tip CI red", retirement
    assert events(case)[-1]["result"] == "unused" and events(case)[-1]["reason"] == "tip CI red"


def test_moved_target_still_stops_the_batch_after_an_unrelated_close(batched, monkeypatch):
    case = batched
    base, tip = build_waiting_batch(case, [5, 1])
    close_unrelated(case)
    lock, moved = StateStore.merge_lock, []

    @contextmanager
    def target_moves_under_lock(store):
        with lock(store):
            if not moved:
                moved.append(True)
                case.history.update({})
            yield

    monkeypatch.setattr(StateStore, "merge_lock", target_moves_under_lock)
    green(case)
    support.gate(case)
    assert moved, "the batch must reach its locked landing step"
    assert not case.target_pushes and not case.merges and case.history.tip != tip
    assert not case.store.owner_merge_facts() and not refs(case)
    retirement, = batch.retirements(case.ctx)
    assert SCOPE_WORDS not in retirement["reason"], retirement
    assert not retirement["spent"]
    assert events(case)[-1]["used_members"] == 0


def test_changed_approved_acceptance_file_still_stops_the_batch_after_an_unrelated_close(
        batched, monkeypatch):
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
    case.pending_batch = True
    queue_workers(case, [1])
    case.worker.number = 5
    case.clock[0] += timedelta(seconds=1)
    case.store.start_run(5, branch="foragent/issue-5", worktree=case.store.worktree_path(5))
    case.tracker.add_labels(5, ["running"])
    assert runner.run_issue(5, case.ctx) == "held"
    case.clock[0] += timedelta(seconds=1)
    assert "merge-queued" in merge(case.ctx, support.pr_for(case, 5).number, approved_by="human")
    from foragent import orch_queue
    orch_queue.set_global(case.store, [5, 1])
    support.gate(case)
    assert len(groups(case)) == 1 and refs(case) and not events(case)
    close_unrelated(case)
    green(case)
    support.gate(case)
    assert_not_landed(case, case.base)
    assert not refs(case)
    retirement, = batch.retirements(case.ctx)
    assert "member #1" in retirement["reason"] and own in retirement["reason"], retirement
    assert SCOPE_WORDS not in retirement["reason"]


# ------------------------------------------------------ 4. replay of #2095

def test_replay_of_batch_2095_lands_when_an_issue_closes_between_build_and_landing(
        request, monkeypatch):
    monkeypatch.setattr(contract, "NUMBERS", (5, 1, 4, 2, 6, 3, 7))
    case = request.getfixturevalue("batched")
    case.config = replace(case.config, merge_batch_size=6)
    case.ctx.config = case.config
    members = [5, 1, 4, 2, 6, 3]
    base, tip = build_waiting_batch(case, members)
    built = current_exclusions(case, base, tip, members)
    assert CLOSING in built
    green(case)                            # all CI green, ~10:46 KST
    close_unrelated(case)                  # an issue closes while owner checks run
    case.clock[0] += timedelta(minutes=12)
    assert case.git.ref_sha("origin/main") == base  # main did not move
    support.gate(case)                     # landing, ~10:58 KST
    assert_landed_once(case, base, tip, members)
