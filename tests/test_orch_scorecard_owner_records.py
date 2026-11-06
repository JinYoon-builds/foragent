"""The merge owner records its own CI and merging time, so the four-part split can count it.

These run the real owner paths (single and batch) from the merge-owner test worlds.
"""

from datetime import datetime, timedelta

from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto, queue_workers, gate, pr_for  # noqa: F401
from test_merge_owner_batch import batched  # noqa: F401
import test_merge_owner_batch as batch_support
from foragent import merge_owner


def owner_rows(case, n, name):
    return [row for row in case.store.flow_records()
            if row["kind"] == "activity" and row["issue"] == n
            and row["data"].get("source") == "merge-owner" and row["data"].get("activity") == name]


def minutes(row):
    data = row["data"]
    return (datetime.fromisoformat(data["finished_at"])
            - datetime.fromisoformat(data["started_at"])).total_seconds() / 60


def test_a_refreshed_heads_failing_ci_is_recorded_before_the_repair(proto):
    case = proto
    queue_workers(case, [5])
    pr = pr_for(case, 5)
    before = case.tracker.pr_head_sha(pr.number)
    case.history.update({"src/neighbor.py": "VALUE = 'new main'\n"})
    gate(case)  # The owner refreshes the PR onto the moved main and pushes a new head.
    head = case.tracker.pr_head_sha(pr.number)
    assert head != before, "the owner refreshed the PR"
    case.tracker.set_pr_checks(pr.number, "fail")
    case.clock[0] += timedelta(minutes=6)
    gate(case)  # The new head's first CI failed: the owner sends the issue to repair.
    assert not merge_owner.pending(case.store, 5)
    rows = owner_rows(case, 5, "ci")
    assert [(row["data"]["head"], row["data"]["cause"]) for row in rows] == [(head, "base-changed")]
    assert minutes(rows[0]) >= 6


def test_a_batch_landing_push_is_every_members_merging_time(batched):
    case = batched
    batch_support.queue_workers(case, [5, 1])
    for n in [5, 1]:
        case.tracker.set_pr_checks(pr_for(case, n).number, "pending")

    def slow_push():
        case.clock[0] += timedelta(minutes=2)

    case.after_target_push = slow_push
    gate(case)
    assert len(case.target_pushes) == 1
    for n in [5, 1]:
        rows = owner_rows(case, n, "merging")
        assert len(rows) == 1, f"#{n} records the landing push once"
        assert minutes(rows[0]) == 2
