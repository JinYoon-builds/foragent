"""Availability, transition and generation regressions for the owner tools."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import fcntl
import os
import sys
from threading import Event, Thread, local

import pytest

from acceptance.test_merge_path_real_settings import world  # noqa: F401
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


@pytest.mark.parametrize("reviewer", ["codex", "none"])
@pytest.mark.parametrize("budget", [300, 900])
def test_owner_budget_spent_following_a_moved_base_after_pass_is_base_changed(proto, monkeypatch, reviewer, budget):
    """PASS -> owner handoff -> non-neutral main move: the owner's turn spends its budget
    re-verifying the refreshed head and the last check is cut at the remaining time.
    That budget went to following the base, so the run is base-changed, not too-large (#1514)."""
    from foragent import run_checks
    from foragent.run_checks import CheckResult

    case = proto
    case.config = replace(case.config, reviewer=reviewer)
    case.ctx.config = case.config
    support.queue_workers(case, [5])
    assert json.loads(merge_owner._path(case.ctx, 5).read_text())["review_pass"] is (reviewer != "none")
    case.history.update({"src/neighbor.py": "VALUE = 'moved'\n"})
    reverify = merge_owner._reverify
    limits = []

    def most_budget_used(ctx, place, job, git, head):
        # Earlier full-length checks on the refreshed head left 300 s of the turn's budget.
        case.clock[0] += timedelta(seconds=case.config.max_runtime_minutes * 60 - budget)
        return reverify(ctx, place, job, git, head)

    def cut_at_remaining(n, ctx, command, worktree, log, timeout_s, **kwargs):
        limits.append(timeout_s)
        case.clock[0] += timedelta(seconds=timeout_s)
        return CheckResult(command, None, True, "killed at the run's remaining time", timeout_s)

    monkeypatch.setattr(merge_owner, "_reverify", most_budget_used)
    monkeypatch.setattr(run_checks, "run_done_when", cut_at_remaining)
    support.gate(case)
    assert limits == [budget]
    expected = "timeout" if budget == 900 else "base-changed" if reviewer != "none" else "too-large"
    path = merge_owner._path(case.ctx, 5)
    assert json.loads(path.read_text())["phase"] == expected
    assert not merge_owner.pending(case.store, 5)
    assert retry_marker(case.store.root, 5).exists() is (expected == "base-changed")
    labels = case.tracker.get_issue(5).labels
    assert labels & {"ready", "blocked", "needs-human"} == ({"ready"} if expected == "base-changed" else {"blocked"})
    assert case.tracker.get_pr(support.pr_for(case, 5).number).state == ("closed" if expected == "base-changed" else "open")
    bodies = [comment.body for comment in case.tracker.list_comments(5)]
    assert any("split it" in body for body in bodies) is (expected == "too-large")
    assert [reason for reason in ("too-large", "timeout") if any(f"reason={reason}" in body for body in bodies)] == (
        [] if expected == "base-changed" else [expected])
    # A later owner process must not resume a terminal job or erase its ready/blocked decision.
    support.gate(case)
    assert json.loads(path.read_text())["phase"] == expected
    assert case.tracker.get_issue(5).labels & {"ready", "blocked", "needs-human"} == labels & {"ready", "blocked", "needs-human"}


@pytest.mark.parametrize("cut", ["local-units", "local-collection", "local-baseline", "related-units"])
def test_owner_refresh_check_cut_at_the_run_budget_is_too_large(proto, monkeypatch, cut):
    """reviewer=none, the source base moved: the owner's re-checks of the refreshed head run on the
    run's remaining time, so one cut there is too-large and blocked, never a repair to ready (#1514)."""
    from foragent import ci_local, run_checks
    from foragent.run_checks import CheckResult

    case = proto
    case.config = replace(case.config, reviewer="none")
    case.ctx.config = case.config
    support.queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 'moved'\n"})
    reverify, cuts = merge_owner._reverify, []

    def most_budget_used(ctx, place, job, git, head):
        case.clock[0] += timedelta(seconds=runner._remaining(place.n, ctx) - 300)
        return reverify(ctx, place, job, git, head)

    def timed_out(command, ctx):
        cuts.append(command)
        case.clock[0] += timedelta(seconds=runner._remaining(5, ctx))  # the owner turn's own clock
        return CheckResult(command, None, True, "killed at the run's remaining time", 300)

    monkeypatch.setattr(merge_owner, "_reverify", most_budget_used)
    if cut == "local-units":
        monkeypatch.setattr(ci_local, "verify_local_only_units", lambda attempt, command: timed_out("local units", attempt.ctx))
    elif cut == "local-collection":
        check = ci_local.check
        monkeypatch.setattr(ci_local, "_uses_local_only", lambda worktree: True)
        monkeypatch.setattr(ci_local, "check", lambda n, ctx, command, root:
                            timed_out(command, ctx) if "--collect-only" in command else check(n, ctx, command, root))
    elif cut == "local-baseline":
        check = ci_local.check
        monkeypatch.setattr(ci_local, "check", lambda n, ctx, command, root:
                            timed_out(command, ctx) if not cuts else check(n, ctx, command, root))
    else:
        monkeypatch.setattr(run_checks, "_related_unit_tests", lambda *args, **kwargs: ["tests/test_unit.py"])
        monkeypatch.setattr(run_checks, "run_related_check", lambda n, ctx, root, command, *args: timed_out(command, ctx))
    support.gate(case)
    assert cuts
    path = merge_owner._path(case.ctx, 5)
    for _ in range(2):  # a later owner turn keeps the result
        assert json.loads(path.read_text())["phase"] == "too-large"
        assert not merge_owner.pending(case.store, 5) and not retry_marker(case.store.root, 5).exists()
        assert case.tracker.get_issue(5).labels & {"ready", "blocked", "needs-human"} == {"blocked"}
        assert case.tracker.get_pr(support.pr_for(case, 5).number).state == "open"
        bodies = [comment.body for comment in case.tracker.list_comments(5)]
        assert any("split it" in body for body in bodies) and any("reason=too-large" in body for body in bodies)
        support.gate(case)


@pytest.mark.parametrize('boundary', ['initial-inputs', 'final-inputs', 'exception', 'withdraw'])
@pytest.mark.parametrize('outcome', ['too-large', 'base-changed', 'runtime-exceeded'])
def test_owner_keeps_terminal_outcome_through_later_handlers(proto, monkeypatch, boundary, outcome):
    from foragent.actions import flag
    from foragent.reasons import Reason
    from foragent.run_attempt import ExecutorStopped

    case = proto
    support.queue_workers(case, [5])
    original = run_publish._final_merge_inputs
    calls = []

    def stopped(n, ctx):
        if outcome == 'runtime-exceeded':
            flag(ctx, n, Reason.RUNTIME_EXCEEDED, 'Runner exceeded its runtime limit.')
            return outcome
        ctx._budget_base_changed = n if outcome == 'base-changed' else None
        return runner._timeout(n, ctx)

    def inputs(n, ctx, *args, **kwargs):
        result = original(n, ctx, *args, **kwargs)
        calls.append(n)
        if len(calls) == (2 if boundary == 'final-inputs' else 1):
            if boundary == 'exception':
                raise ExecutorStopped(stopped(n, ctx))
            return replace(result, stop=lambda: stopped(n, ctx))
        return result

    monkeypatch.setattr(run_publish, '_final_merge_inputs', inputs)
    if boundary == 'withdraw':
        withdraw = merge_owner.queue.withdraw
        interrupted = []

        def lost_withdraw(ctx, place, **kwargs):
            if not interrupted:
                interrupted.append(True)
                raise OSError('Queue withdrawal interrupted')
            return withdraw(ctx, place, **kwargs)

        monkeypatch.setattr(merge_owner.queue, 'withdraw', lost_withdraw)
    support.gate(case)
    path = merge_owner._path(case.ctx, 5)
    assert json.loads(path.read_text())['phase'] == outcome
    assert merge_owner.pending(case.store, 5) is (boundary == 'withdraw')
    assert retry_marker(case.store.root, 5).exists() is (outcome == 'base-changed')
    assert not case.tracker.merged_prs
    assert case.tracker.get_pr(support.pr_for(case, 5).number).state == ('closed' if outcome == 'base-changed' else 'open')
    expected_labels = {'ready'} if outcome == 'base-changed' else {'blocked'}
    assert case.tracker.get_issue(5).labels & {'ready', 'blocked', 'needs-human'} == expected_labels
    bodies = [comment.body for comment in case.tracker.list_comments(5)]
    assert any('split it' in body for body in bodies) is (outcome == 'too-large')
    assert [name for name in ('too-large', 'runtime-exceeded') if any(f'reason={name}' in body for body in bodies)] == (
        [] if outcome == 'base-changed' else [outcome])
    support.gate(case)
    assert not merge_owner.pending(case.store, 5)
    assert json.loads(path.read_text())['phase'] == outcome
    assert case.tracker.get_issue(5).labels & {'ready', 'blocked', 'needs-human'} == expected_labels


@pytest.mark.parametrize("boundary", ["close", "read"])
def test_owner_base_budget_repair_keeps_its_result_after_lost_close_response(proto, monkeypatch, boundary):
    from foragent import run_checks
    from foragent.run_checks import CheckResult

    case = proto
    support.queue_workers(case, [5])
    case.history.update({'src/neighbor.py': 'VALUE = 19\n'})
    verify, close = merge_owner._reverify, case.tracker.close_pr
    closes = []

    def remaining(ctx, place, job, git, head):
        case.clock[0] += timedelta(seconds=case.config.max_runtime_minutes * 60 - 300)
        return verify(ctx, place, job, git, head)

    def cut(n, ctx, command, worktree, log, timeout_s, **kwargs):
        case.clock[0] += timedelta(seconds=timeout_s)
        return CheckResult(command, None, True, 'run budget exhausted', timeout_s)

    def lost(*args, **kwargs):
        close(*args, **kwargs)
        closes.append(args[0])
        if boundary == 'close':
            raise TrackerError('Close response lost')

    monkeypatch.setattr(merge_owner, '_reverify', remaining)
    monkeypatch.setattr(run_checks, 'run_done_when', cut)
    monkeypatch.setattr(case.tracker, 'close_pr', lost)
    if boundary == 'read':
        read, failures = case.tracker.get_issue, []

        def interrupted_read(n):
            job = json.loads(merge_owner._path(case.ctx, 5).read_text())
            if job['phase'] == 'repair' and not failures:
                failures.append(True)
                raise OSError('Issue read interrupted')
            return read(n)

        monkeypatch.setattr(case.tracker, 'get_issue', interrupted_read)
    support.gate(case)
    path = merge_owner._path(case.ctx, 5)
    assert json.loads(path.read_text())['phase'] == 'repair'
    assert json.loads(path.read_text())['repair_outcome'] == 'base-changed'
    assert merge_owner.pending(case.store, 5)
    support.gate(case)
    assert json.loads(path.read_text())['phase'] == 'base-changed'
    assert not merge_owner.pending(case.store, 5)
    assert len(closes) == 1
    assert retry_marker(case.store.root, 5).exists()
    assert case.tracker.get_issue(5).labels & {'ready', 'blocked', 'needs-human'} == {'ready'}


def test_owner_keeps_real_base_block_even_after_following_main(proto, monkeypatch):
    from foragent.actions import flag
    from foragent.reasons import Reason

    case = proto
    support.queue_workers(case, [5])
    original = run_publish._final_merge_inputs

    def inputs(n, ctx, *args, **kwargs):
        result = original(n, ctx, *args, **kwargs)
        ctx._budget_base_changed = n

        def changed_target():
            flag(ctx, n, Reason.RISK_HIGH, 'PR targets an unexpected base; retry implementation.')
            return 'base-changed'

        return replace(result, stop=changed_target)

    monkeypatch.setattr(run_publish, '_final_merge_inputs', inputs)
    support.gate(case)
    assert json.loads(merge_owner._path(case.ctx, 5).read_text())['phase'] == 'base-changed'
    assert not merge_owner.pending(case.store, 5)
    assert case.tracker.get_issue(5).labels & {'ready', 'blocked', 'needs-human'} == {'blocked'}
    assert not retry_marker(case.store.root, 5).exists()
    assert case.tracker.get_pr(support.pr_for(case, 5).number).state == 'open'


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
