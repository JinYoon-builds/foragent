"""Acceptance tests for owner-job-slim-real (UX r1135).

#1957 removed full acceptance reports from the merge-owner job file on the
single path its tests covered. On the real settings (merge_owner, merge_batch,
checks: ci) the owner still writes waiting jobs with comparison.reports at
about 1.8 MB, and merged jobs keep that size forever (164 files, 194 MB).
Every write of merge-owner/N.json, on every owner path, keeps only references
or digests; a job that reaches merged or closed keeps no full report copy.

Real runner, public gate tick, merge owner, merge train, Git, state and
small-repository pytest; only GitHub, models and the clock are fake.
"""

import json

from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from helpers.merge_batch import batched, queue_workers  # noqa: F401
from acceptance.owner_job_slim_real_support import (
    MARK, bodies, job_path, large_reports, legacy, watch_job_writes,
)
from foragent import ci_mode


def real_settings(case):
    assert case.config.merge_owner and case.config.merge_batch and case.config.checks == "ci", (
        "Fixture: the real owner settings")


def merged_once(case, numbers):
    facts = [n for n, _ in case.store.owner_merge_facts()]
    assert sorted(facts) == sorted(numbers), f"Each member merges exactly once: {facts}"
    assert len(case.target_pushes) == 1, "One train landing, not two"


def test_waiting_job_written_by_the_owner_holds_no_report_bodies(batched, monkeypatch):
    """Statement 1: the owner's writes of a PR waiting in its queue keep no report bodies."""
    case = batched
    real_settings(case)
    large_reports(case, monkeypatch)
    queue_workers(case, [5, 1])
    for n in (5, 1):
        legacy(case, n)
    written = watch_job_writes(case, monkeypatch)

    report = support.gate(case)

    waiting = [row for row in written if row["phase"] == "waiting"]
    carried = [row for row in waiting if row["body"]]
    assert not carried, (
        f"The owner wrote waiting jobs that still hold the full reports: {carried}")
    assert not [row for row in written if row["body"]], (
        f"No owner write of a job file holds a report body: {[r for r in written if r['body']]}")
    merged_once(case, [5, 1])
    assert report is not None


def test_waiting_job_file_on_disk_after_a_tick_holds_no_report_bodies(batched, monkeypatch):
    """Statement 1 on disk: while the train CI is pending, the waiting job file is small after a tick."""
    case = batched
    real_settings(case)
    large_reports(case, monkeypatch)
    case.pending_batch = True
    queue_workers(case, [5, 1])
    for n in (5, 1):
        legacy(case, n)

    for _ in range(2):
        support.gate(case)
        phases = {n: json.loads(job_path(case, n).read_text()).get("phase") for n in (5, 1)}
        assert set(phases.values()) == {"waiting"}, f"Fixture: both PRs still wait in the queue: {phases}"
        assert not bodies(case), (
            f"Waiting job files on disk still hold the full reports after a tick: {bodies(case)}")
        for n in (5, 1):
            assert json.loads(job_path(case, n).read_text()).get("comparison"), (
                f"#{n} keeps its comparison facts (references or digest), not nothing")
    assert not case.store.owner_merge_facts(), "Nothing merges while the train CI is pending"
    assert not case.target_pushes, "No landing while the train CI is pending"


def test_waiting_job_keeps_its_report_digest(batched, monkeypatch):
    """Statement 1, wrong value: the waiting job keeps the digest of the reports CI measured."""
    case = batched
    real_settings(case)
    large_reports(case, monkeypatch)
    case.pending_batch = True
    queue_workers(case, [5, 1])
    heads = {}
    for n in (5, 1):
        heads[n] = json.loads(legacy(case, n).read_text())["comparison"]["head_sha"]

    support.gate(case)

    # Observe only the job files on disk after the pending tick, whatever the write mechanism.
    for n in (5, 1):
        job = json.loads(job_path(case, n).read_text())
        assert job.get("phase") == "waiting", f"Fixture: #{n} still waits in the queue: {job.get('phase')}"
        assert MARK not in json.dumps(job), f"#{n}'s waiting job file on disk still holds the report body"
        comparison = job.get("comparison")
        assert comparison, f"#{n} keeps its comparison facts (references or digest), not nothing"
        assert comparison.get("head_sha") == heads[n], (
            f"#{n} keeps the head its comparison was measured on, not another value")
        if comparison.get("reports") is not None:
            expected = ci_mode.reports_digest(case.tracker.pr_acceptance_reports(heads[n]))
            assert comparison["reports"] == expected, (
                f"#{n} keeps the digest of the measured reports, not another value")
    assert not case.store.owner_merge_facts(), "Nothing merges while the train CI is pending"


def test_merged_train_members_hold_no_report_bodies(batched, monkeypatch):
    """Statement 2: after the train lands, no member's job file holds a report body."""
    case = batched
    real_settings(case)
    large_reports(case, monkeypatch)
    queue_workers(case, [5, 1])
    for n in (5, 1):
        legacy(case, n)

    support.gate(case)

    merged_once(case, [5, 1])
    assert not bodies(case), f"Merged jobs still keep the full reports: {bodies(case)}"
    for _ in range(2):
        support.gate(case)  # Later ticks neither restore the copy nor merge again.
    merged_once(case, [5, 1])
    assert not bodies(case), f"Merged jobs still keep the full reports: {bodies(case)}"


def test_closed_job_holds_no_report_bodies(batched, monkeypatch):
    """Goal: a job that reaches closed keeps no full report copy either."""
    case = batched
    real_settings(case)
    large_reports(case, monkeypatch)
    case.pending_batch = True
    queue_workers(case, [5, 1])
    for n in (5, 1):
        legacy(case, n)
    case.tracker.close_issue(5)

    support.gate(case)

    job = json.loads(job_path(case, 5).read_text()) if job_path(case, 5).exists() else {}
    assert job.get("phase", "closed") == "closed", f"Fixture: #5 ends closed, got {job.get('phase')}"
    assert "5.json" not in bodies(case), f"The closed job still keeps the full reports: {bodies(case)}"
    assert not case.store.owner_merge_facts(), "Closing merges nothing"


def test_job_merged_before_the_fix_is_slimmed_by_the_owner(batched, monkeypatch):
    """Statement 2 on disk: a merged job written by the old code (1957.json, 2603.json) shrinks."""
    case = batched
    real_settings(case)
    large_reports(case, monkeypatch)
    queue_workers(case, [5, 1])
    support.gate(case)
    merged_once(case, [5, 1])
    for n in (5, 1):
        assert json.loads(job_path(case, n).read_text())["phase"] == "merged", "Fixture: merged jobs"
        legacy(case, n)
    assert bodies(case), "Fixture: the old merged jobs hold the full reports"

    support.gate(case)

    assert not bodies(case), f"Merged jobs from before the fix still keep the full reports: {bodies(case)}"
    merged_once(case, [5, 1])
