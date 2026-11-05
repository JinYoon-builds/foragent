"""Durable evidence handoff, conservative migration and append serialization."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
import fcntl
import json
import os
from threading import Event, Thread, current_thread
from types import SimpleNamespace

import pytest

from acceptance.test_merge_path_real_settings import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import ci_mode, merge_owner, run_merge_turn as queue, run_publish
from foragent.config import Config
from foragent.state import Approval, StateStore


NOW = datetime(2026, 11, 4, tzinfo=UTC)


@pytest.fixture
def admission(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(7, branch="issue-7", worktree=store.worktree_path(7))
    approval = Approval(7, "Approved work", "body-sha", {}, "human", NOW.isoformat())
    evidence = ci_mode.ComparisonEvidence("base", "head", {}, None, "pass", "")
    # Only the already-tested admission policy read is substituted in this storage test.
    monkeypatch.setattr(run_publish, "_final_merge_inputs", lambda *a, **kw: SimpleNamespace(
        stop=None, decision=None, batch_branch=None, status="pass", base_sha="base", comparison_current=True))
    monkeypatch.setattr(run_publish, "_policy_allows", lambda *a, **kw: True)
    ctx = SimpleNamespace(store=store, now=lambda: NOW, merge_authorization=None,
                          tracker=SimpleNamespace(remove_labels=lambda *a: None))
    return ctx, approval, evidence


def enqueue(admission):
    ctx, approval, evidence = admission
    return merge_owner.enqueue(ctx, 7, None, 17, "issue-7", "main", "head", "base",
                               approval, NOW.timestamp(), evidence)


def test_enqueue_persists_large_evidence_before_small_queue_row(admission, monkeypatch):
    ctx, approval, evidence = admission
    evidence = replace(evidence, reports="x" * 1_280_000)
    admission = ctx, approval, evidence
    append = queue._append
    observed = []

    def inspect(store, row):
        job = json.loads(merge_owner._path(ctx, 7).read_text())
        assert job["started_at"] == row["started_at"]
        assert job["comparison"] == asdict(evidence)
        assert row["publication"] == {key: value for key, value in job.items()
                                      if key not in {"comparison", "started_at"}}
        observed.append(True)
        append(store, row)

    monkeypatch.setattr(queue, "_append", inspect)
    assert enqueue(admission) == "merge-queued"
    assert observed == [True]
    assert all(len(line) < 10_000 for line in (ctx.store.root / queue.QUEUE).read_bytes().splitlines())
    # A repeated handoff must retain the owner's progress and original queue position.
    path = merge_owner._path(ctx, 7)
    job = json.loads(path.read_text())
    job.update(phase="publishing", head="refreshed")
    ctx.store._write_json(path, job)
    assert enqueue(admission) == "merge-queued"
    assert json.loads(path.read_text()) == job and len(queue._rows(ctx.store)) == 1


def test_failed_job_write_never_admits(admission, monkeypatch):
    ctx, *_ = admission

    def fail(*args):
        raise OSError("job storage unavailable")

    monkeypatch.setattr(ctx.store, "_write_json", fail)
    with pytest.raises(OSError, match="job storage"):
        enqueue(admission)
    assert queue._rows(ctx.store) == []


def test_owner_merges_with_job_file_evidence_without_remeasurement(proto, monkeypatch):
    case = proto
    support.queue_workers(case, [5])
    assert "comparison" not in next(iter(merge_owner.admissions(case.store).values()))["publication"]

    def unexpected(*args, **kwargs):
        pytest.fail("A current job-file comparison should be reused")

    monkeypatch.setattr(ci_mode, "require_comparison", unexpected)
    support.gate(case)
    assert len(case.tracker.merged_prs) == 1
    assert not merge_owner.pending(case.store, 5)


@pytest.mark.parametrize("job_state", ["missing", "other-start", "no-comparison"])
def test_absent_job_evidence_remeasures_and_never_merges_without_it(proto, monkeypatch, job_state):
    case = proto
    support.queue_workers(case, [5])
    path = merge_owner._path(case.ctx, 5)
    job = json.loads(path.read_text())
    # Even an old, uncompacted queue payload is not a substitute for the job file.
    journal = case.store.root / queue.QUEUE
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    next(row for row in rows if row.get("merge_owner"))["publication"]["comparison"] = job["comparison"]
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows))
    if job_state == "missing":
        path.unlink()
    elif job_state == "other-start":
        case.store._write_json(path, {**job, "started_at": "another-execution", "pr": -1})
    else:
        del job["comparison"]
        case.store._write_json(path, job)
    reports = case.tracker.pr_acceptance_reports
    requested = []
    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", lambda head: requested.append(head) or [])
    support.gate(case)
    assert requested, "The owner did not remeasure missing evidence"
    assert not case.tracker.merged_prs and merge_owner.pending(case.store, 5)
    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", reports)
    support.gate(case)
    assert len(case.tracker.merged_prs) == 1


def old_queue(tmp_path):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    row = {"n": 7, "started_at": NOW.isoformat(), "event": "ready", "target": "main",
           "head_sha": "head", "merge_owner": True, "at": NOW.isoformat(),
           "publication": {"comparison": {"reports": "large" * 1000}, "phase": "waiting",
                           "approval": {"body": "보존할 내용"}, "future": [1, {"x": True}]}}
    store._write_json(store.root / "merge-owner/7.json", {
        "started_at": row["started_at"], "comparison": row["publication"]["comparison"]})
    path = store.root / queue.QUEUE
    path.write_text(json.dumps(row, ensure_ascii=False) + "\r\n", newline="")
    return store, path, row


def test_compaction_preserves_order_fields_backup_and_noop_version(tmp_path):
    store, path, first = old_queue(tmp_path)
    rows = [first, {**first, "started_at": "old-execution"}, {**first, "n": 8},
            {**first, "n": 9}, {**first, "n": 10, "started_at": None},
            {"n": 7, "event": "turn", "result": "merged", "future": 42}]
    store._write_json(store.root / "merge-owner/9.json", {"started_at": first["started_at"], "comparison": None})
    store._write_json(store.root / "merge-owner/10.json", {"comparison": {"reports": "unidentified"}})
    original = ("\r\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
                + '\r\ninvalid tail\r\n').encode()
    path.write_bytes(original)
    queue._rows(store)  # A compaction must invalidate the shared queue reader too.
    assert merge_owner.compact_queue(store) == 1
    expected = json.loads(json.dumps(rows))
    del expected[0]["publication"]["comparison"]
    assert queue._rows(store) == expected
    assert path.read_bytes().endswith(b"invalid tail\r\n")
    backups = list(path.parent.glob(f"{queue.QUEUE}.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    before = path.stat()
    assert merge_owner.compact_queue(store) == 0
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert list(path.parent.glob(f"{queue.QUEUE}.bak-*")) == backups


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("body", ["보존할 내용", "보존\u0085할\u2028내\u2029용"])
def test_compaction_preserves_queue_record_boundaries(tmp_path, separator, body):
    store, path, first = old_queue(tmp_path)
    first["publication"]["approval"]["body"] = body
    second = {**first, "n": 8}
    original = separator.join(json.dumps(row, ensure_ascii=False) for row in [first, second]).encode()
    path.write_bytes(original)
    assert queue._rows(store) == [first, second]
    admissions = list(merge_owner.admissions(store))
    assert admissions == [(7, first["started_at"]), (8, second["started_at"])]
    assert merge_owner.compact_queue(store) == 1
    compacted = {**first, "publication": {key: value for key, value in first["publication"].items()
                                        if key != "comparison"}}
    assert queue._rows(store) == [compacted, second]
    assert list(merge_owner.admissions(store)) == admissions
    assert next(path.parent.glob(f"{queue.QUEUE}.bak-*")).read_bytes() == original


def test_compaction_serializes_with_concurrent_append(tmp_path, monkeypatch):
    store, path, first = old_queue(tmp_path)
    original = path.read_bytes()
    reached, finished = Event(), Event()
    blocked, failures = [], []
    flock, atomic_write = fcntl.flock, store._atomic_write
    ctx = SimpleNamespace(store=StateStore(store.root), now=lambda: NOW)

    def append():
        try:
            queue.join(ctx, queue.Seat(8, "next-start", "main"), "next-head")
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    worker = Thread(target=append, daemon=True)

    def observed_flock(fd, operation):
        if current_thread() is worker and operation & fcntl.LOCK_EX:
            try:
                flock(fd, operation | fcntl.LOCK_NB)
                blocked.append(False)
            except BlockingIOError:
                blocked.append(True)
            reached.set()
        return flock(fd, operation)

    def replace(target, content):
        if target == path:
            worker.start()
            assert reached.wait(10), "Appender did not reach the state lock"
            assert blocked == [True] and not finished.is_set()
        return atomic_write(target, content)

    monkeypatch.setattr(fcntl, "flock", observed_flock)
    monkeypatch.setattr(store, "_atomic_write", replace)
    try:
        assert merge_owner.compact_queue(store) == 1
    finally:
        if worker.ident is not None:
            worker.join(10)
    assert finished.is_set() and not failures
    assert [row["n"] for row in queue._rows(store)] == [7, 8]
    assert "comparison" not in queue._rows(store)[0]["publication"]
    assert next(path.parent.glob(f"{queue.QUEUE}.bak-*")).read_bytes() == original


def test_failed_compaction_replace_preserves_original_and_backup(tmp_path, monkeypatch):
    store, path, _ = old_queue(tmp_path)
    original = path.read_bytes()
    replace = os.replace

    def fail(source, target):
        if target == path:
            raise OSError("interrupted replace")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="interrupted"):
        merge_owner.compact_queue(store)
    assert path.read_bytes() == original
    assert next(path.parent.glob(f"{queue.QUEUE}.bak-*")).read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


def test_owner_process_compacts_at_start_even_during_backoff(tmp_path, monkeypatch):
    from datetime import timedelta
    store, path, _ = old_queue(tmp_path)
    store.pause_github(datetime.now(UTC) + timedelta(hours=1))
    monkeypatch.setattr(merge_owner, "reap_checks", lambda *args: None)
    with store.lock_acceptance_baseline("merge-owner") as lock:
        merge_owner.run_process({"repo": "owner/repo", "repo_root": str(tmp_path),
                                 "store_root": str(store.root), "config": asdict(Config(merge_owner=True)),
                                 "committed_config": False}, os.dup(lock.fileno()))
    assert "comparison" not in queue._rows(store)[0]["publication"]
    assert len(list(path.parent.glob(f"{queue.QUEUE}.bak-*"))) == 1
