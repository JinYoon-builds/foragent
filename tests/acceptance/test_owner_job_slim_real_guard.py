"""Regression guards for owner-job-slim-real; these already pass on main (not Done when).

Fresh admissions on the real settings are written small, and the single path
(merge_batch off) slims a legacy job when it merges. The fix must keep both,
and must keep merging each PR once from the stored evidence.
"""

from dataclasses import replace

from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from helpers.merge_batch import batched, queue_workers  # noqa: F401
from acceptance.owner_job_slim_real_support import bodies, large_reports, legacy, watch_job_writes


def test_fresh_train_members_are_written_without_report_bodies(batched, monkeypatch):
    case = batched
    large_reports(case, monkeypatch)
    written = watch_job_writes(case, monkeypatch)
    queue_workers(case, [5, 1])
    support.gate(case)
    assert sorted(n for n, _ in case.store.owner_merge_facts()) == [1, 5]
    assert len(case.target_pushes) == 1
    assert written and not [row for row in written if row["body"]]
    assert not bodies(case)


def test_single_path_legacy_job_merges_once_without_report_bodies(proto, monkeypatch):
    case = proto
    case.config = case.ctx.config = replace(case.config, merge_batch=False)
    large_reports(case, monkeypatch)
    support.queue_workers(case, [5])
    pr = support.pr_for(case, 5).number
    rounds = len(case.ci.by_head)
    legacy(case, 5)
    support.drain(case, count=1)
    assert case.tracker.merged_prs == [pr]
    assert len(case.ci.by_head) == rounds, "The legacy evidence still verifies; no new CI round"
    assert not bodies(case)
