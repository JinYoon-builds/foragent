"""Availability, transition and generation regressions for the owner tools."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import fcntl
import os
import sys
from threading import Event, Thread, local

import pytest

from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import merge_owner, run_publish, runner
from foragent.issue import body_hash
from foragent.git import GitRepo
from foragent.run_attempt import AttemptFailure
from foragent.run_previous import retry_marker
from foragent.state import StateStore
from foragent.state_local import _approval_identity
from foragent.tracker import RejectedEvidence, TrackerError


@pytest.mark.parametrize("moved", [False, True], ids=["same-base", "refresh"])
def test_owner_reads_future_admission_fields_across_restart(proto, moved):
    case = proto
    support.queue_workers(case, [5])
    path = merge_owner._path(case.ctx, 5)
    publication = json.loads(path.read_text())
    publication["approval"]["future_owner_field"] = {"revision": 2}
    assert isinstance(publication["comparison"], dict)
    publication["comparison"]["future_owner_field"] = {"revision": 2}
    path.write_text(json.dumps(publication))
    if moved:
        case.history.update({"src/neighbor.py": "VALUE = 'new base'\n"})
    support.drain(case, count=1)
    assert case.tracker.get_issue(5).state == "closed"
    assert len(case.tracker.merged_prs) == 1
    assert case.store.run_merged(case.store.get_run(5))


@pytest.mark.parametrize("boundary", ["final-check", "merge"])
def test_owner_excludes_main_writers_until_merge_finishes(proto, monkeypatch, boundary):
    from conftest import git_command

    case = proto
    support.queue_workers(case, [5])
    settled, finished = Event(), Event()
    worker = local()
    failures, observations = [], []
    flock = fcntl.flock

    def observed_flock(fd, operation):
        if getattr(worker, "writer", False) and operation & fcntl.LOCK_EX:
            try:
                return flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                settled.set()
        return flock(fd, operation)

    def write():
        worker.writer = True
        try:
            with case.git.publish_lock():
                case.history.update({"src/incoming.py": "VALUE = 17\n"})
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()
            settled.set()

    thread = Thread(target=write, daemon=True)

    def arrive():
        thread.start()
        assert settled.wait(10), "Writer neither completed nor reached the OS lock"
        observations.append(finished.is_set())

    check, merge = GitRepo.check_publication, case.tracker.merge_pr

    def checked(git, base):
        result = check(git, base)
        if boundary == "final-check":
            arrive()
        return result

    def merging(*args, **kwargs):
        if boundary == "merge":
            arrive()
        observations.append(finished.is_set())
        return merge(*args, **kwargs)

    monkeypatch.setattr(fcntl, "flock", observed_flock)
    monkeypatch.setattr(GitRepo, "check_publication", checked)
    monkeypatch.setattr(case.tracker, "merge_pr", merging)
    try:
        support.gate(case)
    finally:
        if thread.ident is not None:
            thread.join(10)
    assert finished.is_set() and not failures, failures
    assert observations == [False, False], "Main changed between verification and merge"
    assert len(case.tracker.merged_prs) == 1
    assert case.tracker.get_issue(5).state == "closed"
    assert git_command(case.history.origin, "show", "main:src/member_5.py") == "VALUE = 5"
    assert git_command(case.history.origin, "show", "main:src/incoming.py") == "VALUE = 17"


@pytest.mark.parametrize("boundary", ["read", "merge"])
@pytest.mark.parametrize("outage", [False, True], ids=["rate-limit", "connection-drop"])
def test_throttle_persists_shared_wait_without_repair(proto, monkeypatch, boundary, outage):
    case = proto
    support.queue_workers(case, [5, 1])
    name = "get_pr" if boundary == "read" else "merge_pr"
    original = getattr(case.tracker, name)
    calls = []
    until = case.clock[0] + timedelta(minutes=2)

    def throttle(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            if outage:
                raise TrackerError("HTTP 502", transport=True)
            raise TrackerError("API rate limit exceeded", stdout=f"x-ratelimit-reset: {int(until.timestamp())}")
        return original(*args, **kwargs)

    monkeypatch.setattr(case.tracker, name, throttle)
    support.gate(case)
    if outage:
        until = case.clock[0] + timedelta(minutes=1)
    assert case.store.github_wait_until(case.clock[0]) == until
    assert len(calls) == 1
    support.gate(case)
    assert len(calls) == 1, "A new pass must honor the durable, shared pause"
    assert len(merge_owner.admissions(case.store)) == 2
    for n in (5, 1):
        assert not retry_marker(case.store.root, n).exists()
        assert not case.tracker.get_issue(n).labels & {"blocked", "needs-human"}
        path = merge_owner._path(case.ctx, n)
        assert not path.exists() or "merge_failure" not in json.loads(path.read_text())
    case.clock[0] = until + timedelta(seconds=1)
    support.drain(case, count=2)
    assert len(case.tracker.merged_prs) == 2


@pytest.mark.parametrize("changed_body", [True, False])
@pytest.mark.parametrize("merged", [True, False])
def test_old_publication_preserves_new_approval(proto, changed_body, merged):
    case = proto
    original = case.store.load_approval(5)
    assert _approval_identity(replace(original, parent_context="Enriched worker context")) == _approval_identity(original)
    support.queue_workers(case, [5])
    admitted = case.store.get_run(5)
    pr = support.pr_for(case, 5)
    if merged:
        case.tracker.merge_pr(pr.number, expected_head_sha=case.tracker.pr_head_sha(pr.number))
    case.clock[0] += timedelta(seconds=2)
    old = case.store.load_approval(5)
    body = old.body + ("\nFollow-up work is now approved.\n" if changed_body else "")
    case.tracker.update_issue(5, title="Approved follow-up", body=body)
    case.tracker.record_label(5, "approved", "human")
    case.tracker.add_labels(5, ["ready"])
    approval = replace(old, body=body, body_sha=body_hash(body), approved_at=case.clock[0].isoformat())
    case.store.save_approval(**approval.__dict__)
    support.gate(case)
    assert case.store.run_merged(admitted) is merged
    assert case.store.load_approval(5) == approval
    assert case.tracker.get_issue(5).state == "open"
    assert "ready" in case.tracker.get_issue(5).labels
    assert not merge_owner.pending(case.store, 5)
    assert support.contract.turns(case) == ([5] if merged else [])


@pytest.mark.parametrize("boundary", ["confirmation", "label"])
def test_repair_intent_survives_confirmation_or_label_failure(proto, monkeypatch, boundary):
    case = proto
    support.queue_workers(case, [5])
    case.history.update({"src/member_5.py": "VALUE = 'conflicting upstream'\n"})
    failed = []
    conflict = []
    changed = run_publish._reviewed_base_changed

    def base_changed(*args):
        result = changed(*args)
        conflict.append(True)
        return result

    monkeypatch.setattr(run_publish, "_reviewed_base_changed", base_changed)
    read = case.tracker.get_pr
    labels = case.tracker.add_labels

    def unavailable(pr):
        if boundary == "confirmation" and conflict and not failed:
            failed.append(True)
            raise TrackerError("HTTP 502")
        return read(pr)

    def label_response_lost(n, values):
        labels(n, values)
        if boundary == "label" and "blocked" in values and not failed:
            failed.append(True)
            raise TrackerError("HTTP 502 after label write")

    monkeypatch.setattr(case.tracker, "get_pr", unavailable)
    monkeypatch.setattr(case.tracker, "add_labels", label_response_lost)
    support.gate(case)
    assert failed
    assert json.loads(merge_owner._path(case.ctx, 5).read_text())["phase"] == "repair"
    assert merge_owner.pending(case.store, 5)
    assert "blocked" in case.tracker.get_issue(5).labels
    support.gate(case)
    assert not merge_owner.pending(case.store, 5)
    assert retry_marker(case.store.root, 5).exists()
    assert "ready" in case.tracker.get_issue(5).labels
    assert "blocked" not in case.tracker.get_issue(5).labels
    assert case.tracker.get_pr(support.pr_for(case, 5).number).state == "closed"


def test_slow_failed_admission_does_not_spend_next_budget(proto, monkeypatch):
    case = proto
    support.queue_workers(case, [5, 1])
    case.history.update({"src/neighbor.py": "VALUE = 'moved'\n"})
    reverify = merge_owner._reverify
    verified = []

    def slow(ctx, place, job, git, head):
        if place.n == 5:
            case.clock[0] += timedelta(minutes=case.config.max_runtime_minutes + 1)
            raise AttemptFailure("First refresh used its budget", "main")
        verified.append((place.n, runner._remaining(place.n, ctx)))
        return reverify(ctx, place, job, git, head)

    monkeypatch.setattr(merge_owner, "_reverify", slow)
    support.gate(case)
    assert verified and verified[0] == (1, case.config.max_runtime_minutes * 60)
    assert merge_owner.pending(case.store, 1)
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    support.drain(case, count=1)
    assert support.contract.turns(case) == [1]


def test_owner_local_fallback_autodetects_without_worker_baseline(proto, monkeypatch, tmp_path):
    case = proto
    support.queue_workers(case, [5])
    # Exercise real command detection while replacing only uv's external launch.
    tool = tmp_path / "bin" / "uv"
    tool.parent.mkdir()
    tool.write_text(f"#!{sys.executable}\nimport os, sys\nassert sys.argv[1:3] == ['run', 'pytest']\n"
                    "os.execv(sys.executable, [sys.executable, '-m', 'pytest', *sys.argv[3:]])\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool.parent) + os.pathsep + os.environ["PATH"])
    case.history.update({"pyproject.toml": '[project]\nname = "sandbox"\nversion = "0.0.0"\n'})
    case.config = replace(case.config, test_command=None, checks_fallback="local")
    case.ctx.config = case.config
    support.gate(case)  # Refresh and invalidate the worker's old CI evidence.

    def rejected(head):
        raise RejectedEvidence("Completed artifact rejected")

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", rejected)
    support.gate(case)
    assert len(case.tracker.merged_prs) == 1, case.store.log_path(5).read_text()
    assert not merge_owner.pending(case.store, 5)


def test_owner_merge_fact_is_bound_and_timestamp_is_parsed(tmp_path):
    clock = [datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    old = store.start_run(1, branch="old", worktree=tmp_path / "old")
    store.finish_run(1, "merge-queued")
    fact = {"pr": 9, "target": "main", "head": "abc", "merged_at": "2026-10-20T09:00:01Z"}
    store.record_owner_merge(1, old.started_at, fact)
    history = (store.root / "runs.jsonl").read_bytes()
    store.record_owner_merge(1, old.started_at, fact)
    assert (store.root / "runs.jsonl").read_bytes() == history
    # Replay must also finish an interrupted file-write/history-append pair.
    (store.root / "runs.jsonl").write_bytes(history.rsplit(b"\n", 2)[0] + b"\n")
    store.record_owner_merge(1, old.started_at, fact)
    assert (store.root / "runs.jsonl").read_bytes() == history
    assert store.run_merged(old)
    clock[0] += timedelta(seconds=1, microseconds=500000)
    new = store.start_run(1, branch="new", worktree=tmp_path / "new")
    assert not store.run_merged(new)
    value = json.loads((store.root / "runs/1.json").read_text())
    assert "owner_merge" not in value and "merged_at" not in value
    # Imported/inherited data must also be rejected, even with the new binding.
    value.update(owner_merge=fact, owner_merge_started_at=new.started_at)
    store._write_json(store.root / "runs/1.json", value)
    assert not store.run_merged(new), "09:00:01Z precedes 09:00:01.500000+00:00"
    value["owner_merge_started_at"] = old.started_at
    value["owner_merge"] = {**fact, "merged_at": "2026-10-20T10:00:00Z"}
    store._write_json(store.root / "runs/1.json", value)
    store.finish_run(1, "merge-queued")
    assert not store.run_merged(new), "A later timestamp cannot move a fact between generations"
    assert (1, new.started_at) not in store.merge_times()
    assert store.merge_times()[1, old.started_at] == fact["merged_at"]


def test_late_owner_merge_fact_updates_its_history_without_changing_new_run(tmp_path):
    clock = [datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    old = store.start_run(1, branch="old", worktree=tmp_path / "old")
    store.update_run(1, last_review_pass_at=clock[0].isoformat())
    clock[0] += timedelta(seconds=1)
    store.finish_run(1, "merge-queued")
    finished = store.finish_times()[1, old.started_at]
    clock[0] += timedelta(seconds=1)
    new = store.start_run(1, branch="new", worktree=tmp_path / "new")
    path, history = store.root / "runs/1.json", store.root / "runs.jsonl"
    newer = path.read_bytes()
    fact = {"pr": 9, "target": "main", "head": "abc", "merged_at": clock[0].isoformat()}

    before = history.read_bytes()
    with pytest.raises(ValueError, match="generation is unavailable"):
        store.record_owner_merge(1, (clock[0] - timedelta(days=1)).isoformat(), fact)
    assert path.read_bytes() == newer and history.read_bytes() == before

    store.record_owner_merge(1, old.started_at, fact)
    assert path.read_bytes() == newer
    assert store.owner_merge_facts() == {(1, old.started_at): fact}
    assert store.run_merged(old) and not store.run_merged(new)
    recorded = history.read_bytes()
    assert len(recorded.splitlines()) == 2
    saved = json.loads(recorded.splitlines()[-1])
    assert saved["outcome"] == "merge-queued" and saved["finished_at"] == finished
    assert saved["review_pass_to_merge_seconds"] == 2
    assert store.finish_times()[1, old.started_at] == finished
    assert store.merge_times() == {(1, old.started_at): fact["merged_at"]}

    # Reconciliation after another restart repeats no append and preserves the new run.
    store = StateStore(store.root, now=lambda: clock[0])
    store.record_owner_merge(1, old.started_at, fact)
    assert history.read_bytes() == recorded and path.read_bytes() == newer
    assert len(store.read_history()) == 1
    clock[0] += timedelta(seconds=1)
    store.finish_run(1, "merge-queued")
    new_fact = {**fact, "pr": 10, "head": "def", "merged_at": clock[0].isoformat()}
    store.record_owner_merge(1, new.started_at, new_fact)
    assert store.owner_merge_facts() == {(1, old.started_at): fact, (1, new.started_at): new_fact}
