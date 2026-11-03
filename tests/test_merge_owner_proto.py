"""Real runner/tick/git/state; only GitHub, models, process table and clock are fake.

Reuse the locked merge-path contract's immutable, actually measured CI transport.
Git snapshots use fast-import/fetch: no git commit/push/reset/checkout/stash runs.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from acceptance import test_merge_path_real_settings as contract
from acceptance.test_merge_path_real_settings import world  # noqa: F401
from conftest import git_command
from foragent import merge_owner, orch_queue, runner
from foragent.config import Config, ConfigError, load_config
from foragent.executors import ExecResult
from foragent.scheduler import TickContext, tick
from foragent.run_previous import retry_marker
from foragent.state import StateStore
from foragent.tracker import FakeTracker, GhTracker, TrackerError


@pytest.fixture
def proto(world, monkeypatch):
    case = world()
    # The shared factual adapter handles branch commits. Extend its external
    # commit transport to detached owner checkouts without executing git commit.
    import_index = contract.import_index

    def detached_import(root, title):
        root = Path(root)
        try:
            git_command(root, "symbolic-ref", "HEAD")
        except subprocess.CalledProcessError:
            ref = f"refs/heads/offline-detached-{uuid4().hex}"
            git_command(root, "update-ref", ref, git_command(root, "rev-parse", "HEAD"))
            git_command(root, "symbolic-ref", "HEAD", ref)
            try:
                import_index(root, title)
            finally:
                git_command(root, "update-ref", "--no-deref", "HEAD", git_command(root, "rev-parse", ref))
                git_command(root, "update-ref", "-d", ref)
        else:
            import_index(root, title)

    monkeypatch.setattr(contract, "import_index", detached_import)
    close_pr = case.tracker.close_pr

    def close_transport(pr, comment=None, *, delete_branch=True):
        if delete_branch:
            return close_pr(pr, comment)
        # The locked fixture predates the preserve-branch tool option; supply
        # its factual transport here without changing the locked contract.
        return FakeTracker.close_pr(case.tracker, pr, comment, delete_branch=False)

    monkeypatch.setattr(case.tracker, "close_pr", close_transport)
    case.config = replace(case.config, merge_owner=True)
    case.ctx.config = case.config
    case.store.finish_run(contract.BLOCKER, outcome="merged")
    for n in contract.NUMBERS:
        case.store.finish_run(n, outcome="fixture-unused")
        case.tracker.remove_labels(n, ["running"])
    case.armed = True
    # No time wait or model invocation is available to a queued publication.
    case.ctx.sleep = lambda seconds: pytest.fail(f"Worker kept polling: {seconds}")
    case.launches = []
    case.owner_starts = []
    case.launch_owner = merge_owner.start

    def start_owner(ctx, report):
        # Emulate only the process transport. Execute the real owner after the
        # public scheduler returns and releases tick.lock.
        case.owner_starts.append(ctx)

    monkeypatch.setattr(merge_owner, "start", start_owner)
    return case


def queue_workers(case, numbers=contract.NUMBERS):
    def work(n):
        case.worker.number = n
        return runner.run_issue(n, case.ctx)

    # A dedicated executor thread represents the runner boundary. Shutdown joins
    # it before any owner tick; the queued PR has no runner thread/process/slot.
    with ThreadPoolExecutor(max_workers=1) as pool:
        for n in numbers:
            case.clock[0] += timedelta(seconds=1)
            case.store.start_run(n, branch=f"foragent/issue-{n}", worktree=case.store.worktree_path(n))
            case.tracker.add_labels(n, ["running"])
            assert pool.submit(work, n).result(timeout=60) == "merge-queued", case.store.log_path(n).read_text()
    assert all(not thread.is_alive() for thread in pool._threads)
    for n in numbers:
        assert case.store.get_run(n).status == "finished"
        assert not case.store.worktree_path(n).exists()


def gate(case, *, start_runs=False, **options):
    # Use the public tick with fake launcher and no admission of new work. A new
    # context and store each time make reliance on an in-memory queue fail.
    case.clock[0] += timedelta(seconds=1)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.ctx.store = case.store
    report = tick(TickContext(
        "owner/repo", case.root, case.tracker, case.store, case.config, case.git,
        now=lambda: case.clock[0], launcher=lambda n: case.launches.append(n), start_runs=start_runs,
        baseline_launcher=lambda *args: None, **options,
    ))
    while case.owner_starts:
        ctx = case.owner_starts.pop(0)
        with ctx.store.tick_lock() as released:
            assert released, "Owner work still runs under tick.lock"
        lock = ctx.store.lock_acceptance_baseline("merge-owner", blocking=False)
        if lock is not None:
            with lock:
                merge_owner.tick(ctx, report)
    return report


def drain(case, count=4):
    for _ in range(2 * count + 3):
        gate(case)
        if not merge_owner.admissions(case.store):
            return
    pytest.fail("Merge owner did not drain its queue")


def pr_for(case, n):
    return next(pr for pr in reversed(case.tracker.created_prs) if pr.head == f"foragent/issue-{n}")


def test_four_prs_priority_without_live_workers(proto):
    case = proto
    queue_workers(case)
    assert not case.store.active_runs()
    assert len(merge_owner.admissions(case.store)) == 4
    assert not case.tracker.merged_prs
    orch_queue.set_global(case.store, [2, 4, 1, 5])
    drain(case)
    assert contract.turns(case) == [2, 4, 1, 5]
    assert len(case.tracker.merged_prs) == 4
    for n in contract.NUMBERS:
        assert case.tracker.get_issue(n).state == "closed"
        assert git_command(case.history.origin, "show", f"main:src/member_{n}.py") == f"VALUE = {n}"
    assert not case.store.active_runs() and not case.launches


def test_main_moves_between_turns(proto):
    case = proto
    queue_workers(case)
    gate(case)
    assert contract.turns(case) == [5]
    case.history.update({"src/neighbor.py": "VALUE = 'new main'\n"})
    drain(case)
    assert contract.turns(case) == list(contract.NUMBERS)
    assert git_command(case.history.origin, "show", "main:src/neighbor.py") == "VALUE = 'new main'"


def test_lost_test_never_enters_queue(world):
    case = world(scenario="lost")
    case.config = replace(case.config, merge_owner=True)
    case.ctx.config = case.config
    case.store.finish_run(contract.BLOCKER, outcome="merged")
    case.worker.number = 5
    assert runner.run_issue(5, case.ctx) == "retries-exceeded"
    assert not contract.ready(case, 5)
    assert not case.tracker.merged_prs
    assert "Acceptance baseline regression" in case.store.log_path(5).read_text()


def test_new_base_lost_test_uses_existing_repair_path(proto):
    case = proto
    queue_workers(case, [5])
    new_contract = "tests/acceptance/test_new_contract.py"
    case.history.update({new_contract:
        "from pathlib import Path\ndef test_main_contract():\n    assert not Path('src/member_5.py').exists()\n"})
    gate(case)  # Clean refresh, required local checks, new CI head.
    gate(case)  # Green check status but an actual passing test was lost.
    assert not merge_owner.admissions(case.store)
    assert not case.tracker.merged_prs
    assert "ready" in case.tracker.get_issue(5).labels
    assert case.tracker.get_issue(pr_for(case, 5).number).state == "closed"
    assert retry_marker(case.store.root, 5).exists()
    comments = "\n".join(comment.body for comment in case.tracker.list_comments(5))
    assert "Acceptance baseline regression" in comments
    assert new_contract in comments


def test_conflict_goes_to_repair_without_queued_worker(proto):
    case = proto
    queue_workers(case, [5])
    case.history.update({"src/member_5.py": "VALUE = 'main owns this'\n"})
    gate(case)
    assert not merge_owner.admissions(case.store)
    assert not case.tracker.merged_prs
    assert "ready" in case.tracker.get_issue(5).labels
    assert not case.store.active_runs()
    assert git_command(case.history.origin, "show", "main:src/member_5.py") == "VALUE = 'main owns this'"

    class RepairModel:
        def __init__(self, name):
            self.name = name

        def run(self, **kwargs):
            if self.name != "codex":
                (kwargs["worktree"] / "src/member_5.py").write_text("VALUE = 5\n")
            return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

    old_pr = pr_for(case, 5).number
    case.ctx.executor_factory = RepairModel
    # Scheduler's ordinary retry admission creates the next run. The fake
    # launcher records that only this issue needs a worker, then we drive it.
    for n in (*contract.NUMBERS[1:], 77):
        case.tracker.remove_labels(n, ["approved", "ready"])
    gate(case, start_runs=True)
    assert case.launches == [5]
    case.worker.number = 5
    assert runner.run_issue(5, case.ctx) == "merge-queued", case.store.log_path(5).read_text()
    assert pr_for(case, 5).number != old_pr
    assert not retry_marker(case.store.root, 5).exists()
    drain(case, count=1)
    assert case.tracker.get_issue(5).state == "closed"
    assert len(case.tracker.merged_prs) == 1


def test_manually_closed_issue_drops_queue_without_ghost(proto):
    case = proto
    queue_workers(case)
    case.tracker.close_issue(1)
    drain(case)
    assert contract.turns(case) == [5, 4, 2]
    assert not merge_owner.admissions(case.store)
    assert not case.store.active_runs()
    for _ in range(2):
        gate(case)
    assert not case.launches


def test_restart_after_merge_response_lost(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    merge = case.tracker.merge_pr

    def lost_response(*args, **kwargs):
        merge(*args, **kwargs)
        raise TrackerError("Connection lost after GitHub accepted merge")

    monkeypatch.setattr(case.tracker, "merge_pr", lost_response)
    gate(case)
    assert len(case.tracker.merged_prs) == 1
    assert merge_owner.admissions(case.store)
    monkeypatch.setattr(case.tracker, "merge_pr", lambda *args, **kwargs: pytest.fail("Duplicate merge"))
    gate(case)
    assert not merge_owner.admissions(case.store)
    assert case.tracker.get_issue(5).state == "closed"
    assert contract.turns(case) == [5]


def test_ci_never_reports_releases_gate_without_wait_limit(proto):
    case = proto
    queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 7\n"})
    gate(case)
    case.tracker.set_pr_checks(pr_for(case, 5).number, "pending")
    case.clock[0] += timedelta(days=2)
    for _ in range(2):
        gate(case)
    assert merge_owner.admissions(case.store)
    assert not case.tracker.merged_prs
    assert not case.store.active_runs() and not case.launches


def test_two_gates_share_existing_tick_lock(proto):
    case = proto
    queue_workers(case, [5])
    with case.store.tick_lock() as acquired:
        assert acquired
        assert gate(case).skipped == "another tick is running"
    assert not case.tracker.merged_prs
    gate(case)
    assert len(case.tracker.merged_prs) == 1


def test_pr_merged_by_hand_is_reconciled(proto):
    case = proto
    queue_workers(case, [5])
    pr = pr_for(case, 5)
    case.tracker.merge_pr(pr.number, expected_head_sha=case.tracker.pr_head_sha(pr.number))
    gate(case)
    assert not merge_owner.admissions(case.store)
    assert len(case.tracker.merged_prs) == 1
    assert case.tracker.get_issue(5).state == "closed"


def test_restart_during_refresh_publication(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 11\n"})
    original = case.git._run
    failed = []

    def transport(argv):
        args = list(argv)
        while args[:1] == ["-C"]:
            args = args[2:]
        if args[:1] == ["push"] and not failed:
            failed.append(True)
            raise TrackerError("Interrupted before refresh ref update")
        return original(argv)

    monkeypatch.setattr(case.git, "_run", transport)
    gate(case)
    assert json.loads((case.store.root / "merge-owner/5.json").read_text())["phase"] == "publishing"
    gate(case)
    drain(case, count=1)
    assert len(case.tracker.merged_prs) == 1
    assert git_command(case.history.origin, "show", "main:src/neighbor.py") == "VALUE = 11"


def test_local_mode_keeps_local_evidence_through_main_move(proto, monkeypatch):
    case = proto
    case.config = replace(case.config, checks="local")
    case.ctx.config = case.config
    # Local mode has no portable artifact producer at the external CI boundary.
    monkeypatch.setattr(case.ci, "reports", lambda row: [])
    queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 12\n"})
    drain(case, count=1)
    assert len(case.tracker.merged_prs) == 1
    assert not case.ci.reads
    assert git_command(case.history.origin, "show", "main:src/neighbor.py") == "VALUE = 12"


def test_ci_mode_runs_local_only_after_refresh(proto):
    case = proto
    queue_workers(case, [5])
    case.history.update({"tests/acceptance/test_machine.py":
        "from pathlib import Path\nimport pytest\n@pytest.mark.local_only\n"
        "def test_machine():\n    assert not Path('src/member_5.py').exists()\n"})
    old_head = case.tracker.pr_head_sha(pr_for(case, 5).number)
    gate(case)
    assert not merge_owner.admissions(case.store)
    assert not case.tracker.merged_prs
    assert "ready" in case.tracker.get_issue(5).labels
    # The machine check rejects the merge before publishing another CI head.
    assert len(case.ci.by_head) == 1 and old_head in case.ci.by_head
    assert "test_machine" in "\n".join(comment.body for comment in case.tracker.list_comments(5))


def test_missing_ci_comparison_waits_across_ticks(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 13\n"})
    gate(case)
    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", lambda head: [])
    for _ in range(2):
        gate(case)
    assert merge_owner.admissions(case.store)
    assert not case.tracker.merged_prs and not case.store.active_runs()


def test_orch_merge_uses_reviewed_handoff(proto):
    from foragent.orch import merge

    case = proto
    queue_workers(case, [5])
    case.clock[0] += timedelta(seconds=1)
    assert "merge-queued" in merge(case.ctx, pr_for(case, 5).number, approved_by="human")
    assert not case.tracker.merged_prs
    assert case.store.get_run(5).status == "finished"
    drain(case, count=1)
    assert len(case.tracker.merged_prs) == 1


def test_refused_merge_reconciles_before_requesting_repair(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    requests = []

    def refuse(*args, **kwargs):
        requests.append(args)
        raise TrackerError("Merge refused by GitHub")

    monkeypatch.setattr(case.tracker, "merge_pr", refuse)
    gate(case)
    assert merge_owner.admissions(case.store)
    gate(case)
    assert not merge_owner.admissions(case.store)
    assert len(requests) == 1
    assert "ready" in case.tracker.get_issue(5).labels
    assert retry_marker(case.store.root, 5).exists()


def test_main_move_at_handoff_is_owner_work(proto, monkeypatch):
    case = proto
    reports = case.tracker.pr_acceptance_reports
    moved = []

    def move_after_comparison(head):
        evidence = reports(head)
        if not moved:
            moved.append(True)
            case.history.update({"src/at_handoff.py": "VALUE = 14\n"})
        return evidence

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", move_after_comparison)
    queue_workers(case, [5])
    assert case.model_calls == {5: 1}
    assert merge_owner.admissions(case.store)
    drain(case, count=1)
    assert len(case.tracker.merged_prs) == 1
    assert git_command(case.history.origin, "show", "main:src/at_handoff.py") == "VALUE = 14"


@pytest.mark.parametrize("change", ["head", "approval", "issue-closed", "ci-failed", "main"])
def test_final_recheck_under_lock(proto, monkeypatch, change):
    case = proto
    queue_workers(case, [5])
    original = case.store.merge_lock

    @contextmanager
    def changing_lock():
        if change == "head":
            case.tracker.set_pr_head_sha(pr_for(case, 5).number, "f" * 40)
        elif change == "approval":
            case.tracker.remove_labels(5, ["approved"])
        elif change == "issue-closed":
            case.tracker.close_issue(5)
        elif change == "ci-failed":
            case.tracker.set_pr_checks(pr_for(case, 5).number, "fail")
        else:
            case.history.update({"src/late_main.py": "VALUE = 9\n"})
        with original():
            yield

    # gate() deliberately reloads the store, so patch the class lock boundary.
    monkeypatch.setattr(StateStore, "merge_lock", lambda self: changing_lock())
    gate(case)
    assert not case.tracker.merged_prs


def test_merge_owner_is_validated_opt_in(tmp_path):
    assert Config().merge_owner is False
    (tmp_path / ".foragent.yml").write_text("merge_owner: true\ntest_command: uv run pytest\n")
    assert load_config(tmp_path, global_path=tmp_path / "absent").merge_owner is True
    (tmp_path / ".foragent.yml").write_text("merge_owner: yes-please\n")
    with pytest.raises(ConfigError, match="merge_owner must be a boolean"):
        load_config(tmp_path, global_path=tmp_path / "absent")


def update_pr_branch(case, n):
    """Another actor publishes a real commit without any forbidden Git command."""
    pr = pr_for(case, n)
    previous = git_command(case.history.origin, "rev-parse", pr.head)
    content = "VALUE = 'another actor'\n"
    git_command(case.history.origin, "fast-import", "--quiet", input=(
        f"commit refs/heads/{pr.head}\ncommitter Other <other@example.com> 1792483201 +0000\n"
        f"data 6\nUpdate\nfrom {previous}\nM 100644 inline src/another.py\n"
        f"data {len(content)}\n{content}\n\ndone\n"))
    head = git_command(case.history.origin, "rev-parse", pr.head)
    case.tracker.set_pr_head_sha(pr.number, head)
    return head


@pytest.mark.parametrize("rate_limit", [True, False])
@pytest.mark.parametrize("entrypoint", ["implementation", "review-only"])
def test_durable_handoff_survives_label_cleanup_failure(proto, monkeypatch, rate_limit, entrypoint):
    case = proto
    if entrypoint == "review-only":
        queue_workers(case, [5])
    remove = case.tracker.remove_labels
    failed = []

    def interrupted(n, labels):
        if n == 5 and merge_owner.pending(case.store, n) and not failed:
            failed.append(True)
            raise TrackerError("API rate limit exceeded" if rate_limit else "connection reset by peer")
        return remove(n, labels)

    monkeypatch.setattr(case.tracker, "remove_labels", interrupted)
    case.worker.number = 5
    case.clock[0] += timedelta(seconds=1)
    if entrypoint == "implementation":
        case.store.start_run(5, branch="foragent/issue-5", worktree=case.store.worktree_path(5))
        case.tracker.add_labels(5, ["running"])
        outcome = runner.run_issue(5, case.ctx)
    else:
        outcome = runner.review_only(5, case.ctx, verdict_head=case.tracker.pr_head_sha(pr_for(case, 5).number))
    assert outcome in {"limit-wait", "error"}
    run = case.store.get_run(5)
    assert run.status == "finished" and run.outcome != "merge-queued"
    assert merge_owner.pending(case.store, 5)
    head = case.tracker.pr_head_sha(pr_for(case, 5).number)
    case.clock[0] += timedelta(hours=2)  # The shared GitHub backoff has expired.
    drain(case, count=1)
    assert case.tracker.get_issue(5).state == "closed"
    assert len(case.tracker.merged_prs) == 1 and not case.launches
    assert case.merges[0]["head"] == head


@pytest.mark.parametrize("boundary", ["merge-request", "repair-replay", "ci-failure", "retry-advice"])
def test_changed_head_never_enters_destructive_repair(proto, monkeypatch, boundary):
    from foragent import orch_advice

    case = proto
    queue_workers(case, [5])
    pr = pr_for(case, 5)
    pushed = []
    if boundary in {"merge-request", "repair-replay"}:
        def refuse(*args, **kwargs):
            if boundary == "merge-request":
                pushed.append(update_pr_branch(case, 5))
            raise TrackerError("Head SHA does not match --match-head-commit")

        monkeypatch.setattr(case.tracker, "merge_pr", refuse)
        gate(case)
        assert merge_owner.pending(case.store, 5)
        if boundary == "repair-replay":
            path = case.store.root / "merge-owner/5.json"
            job = json.loads(path.read_text())
            job.update(phase="repair", failure="Interrupted durable repair")
            path.write_text(json.dumps(job))
            pushed.append(update_pr_branch(case, 5))
    elif boundary == "ci-failure":
        def failed_logs(*args, **kwargs):
            pushed.append(update_pr_branch(case, 5))
            return "A required check failed"

        case.tracker.set_pr_checks(pr.number, "fail")
        monkeypatch.setattr(case.tracker, "pr_failed_logs", failed_logs)
    else:
        case.tracker.set_pr_checks(pr.number, "fail")
        publish = orch_advice.publish

        def changed_during_advice(*args, **kwargs):
            pushed.append(update_pr_branch(case, 5))
            return publish(*args, **kwargs)

        monkeypatch.setattr(orch_advice, "publish", changed_during_advice)
    gate(case)
    assert pushed
    assert not merge_owner.pending(case.store, 5)
    assert case.tracker.get_issue(pr.number).state == "open"
    assert git_command(case.history.origin, "rev-parse", pr.head) == pushed[-1]
    assert not case.tracker.deleted_branches and not case.tracker.merged_prs
    assert "blocked" in case.tracker.get_issue(5).labels
    assert "ready" not in case.tracker.get_issue(5).labels
    assert not retry_marker(case.store.root, 5).exists()


@pytest.mark.parametrize("review", ["careful"])
def test_explicit_human_authorization_survives_policy_and_restart(proto, monkeypatch, review):
    from foragent.orch import merge

    case = proto
    queue_workers(case, [5])
    case.config = replace(case.config, review=review)
    case.ctx.config = case.config
    case.clock[0] += timedelta(seconds=1)
    assert "merge-queued" in merge(case.ctx, pr_for(case, 5).number, approved_by="human")
    row = merge_owner.admissions(case.store)[(5, case.store.get_run(5).started_at)]
    assert row["publication"]["authorization"]["approved_by"] == "human"
    case.history.update({"src/neighbor.py": "VALUE = 20\n"})
    installed = []
    monkeypatch.setattr("foragent.orch_reinstall._reinstall", lambda ctx, base: installed.append(base))
    drain(case, count=1)
    assert len(case.tracker.merged_prs) == 1
    assert installed == ["main"]
    assert git_command(case.history.origin, "show", "main:src/neighbor.py") == "VALUE = 20"


@pytest.mark.parametrize("change", ["head", "approval", "ci"])
def test_human_authorization_retains_live_integrity_checks(proto, change):
    from foragent.orch import merge

    case = proto
    queue_workers(case, [5])
    case.config = replace(case.config, review="careful")
    case.ctx.config = case.config
    case.clock[0] += timedelta(seconds=1)
    assert "merge-queued" in merge(case.ctx, pr_for(case, 5).number, approved_by="human")
    if change == "head":
        update_pr_branch(case, 5)
    elif change == "approval":
        case.tracker.remove_labels(5, ["approved"])
    else:
        case.tracker.set_pr_checks(pr_for(case, 5).number, "fail")
    gate(case)
    assert not case.tracker.merged_prs


def test_batch_child_merge_never_installs_unapproved_branch(proto, monkeypatch):
    """Batch review is retired: a parented child publishes to the configured base, never to an old batch ref."""
    case = proto
    case.config = replace(case.config, checks="local")
    case.ctx.config = case.config
    case.tracker.add_issue(90, "Parent order")
    case.tracker.set_parent(5, 90)
    case.tracker.set_parent(1, 90)  # Another child is still open.
    branch = "foragent/batch-90"
    stale = git_command(case.history.origin, "rev-parse", "main")
    git_command(case.history.origin, "update-ref", f"refs/heads/{branch}", stale)
    installed = []
    monkeypatch.setattr("foragent.orch_reinstall._reinstall", lambda ctx, base: installed.append(base))
    monkeypatch.setattr(case.ci, "reports", lambda row: [])
    queue_workers(case, [5])
    drain(case, count=1)
    assert pr_for(case, 5).base == "main"
    assert case.tracker.get_issue(5).state == "closed"
    assert case.tracker.get_issue(90).state == "open"
    assert installed == ["main"]
    assert git_command(case.history.origin, "show", "main:src/member_5.py") == "VALUE = 5"
    assert git_command(case.history.origin, "rev-parse", branch) == stale
    # All publication callers share this final-base installation boundary.
    runner.after_merge(case.ctx, pr_for(case, 5).number, branch)
    assert installed == ["main"]


@pytest.mark.parametrize("message,fallback", [("connection reset by peer", None),
                                             ("artifact read: HTTP 404", "local")])
def test_nonwaiting_comparison_transport_retains_admission(proto, monkeypatch, message, fallback):
    case = proto
    case.config = replace(case.config, checks_fallback=fallback)
    case.ctx.config = case.config
    queue_workers(case, [5, 1])
    case.history.update({"src/neighbor.py": "VALUE = 21\n"})
    gate(case)  # Refresh #5; it now needs a new comparison.
    head = case.tracker.pr_head_sha(pr_for(case, 5).number)
    reports = case.tracker.pr_acceptance_reports
    failed = []

    def unavailable(sha):
        if sha == head and not failed:
            failed.append(True)
            raise TrackerError(message)
        return reports(sha)

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", unavailable)
    gate(case)
    assert failed and merge_owner.pending(case.store, 5)
    assert not case.tracker.get_issue(5).labels & {"blocked", "needs-human"}
    assert not retry_marker(case.store.root, 5).exists()
    # The other admission advanced despite this one's transport failure.
    assert case.tracker.pr_head_sha(pr_for(case, 1).number) != contract.ready(case, 1)[0]["head_sha"]
    drain(case, count=2)
    assert len(case.tracker.merged_prs) == 2


def test_reconciliation_reads_stored_pr_numbers_with_real_gh_adapter(proto, monkeypatch):
    case = proto
    queue_workers(case)
    direct = case.tracker.get_pr
    calls = []

    def api(args, input=None):
        calls.append(args)
        assert args[0] == "api" and "--paginate" not in args
        prefix = "repos/owner/repo/pulls/"
        assert args[1].startswith(prefix)
        pr = direct(int(args[1].removeprefix(prefix)))
        return json.dumps({"number": pr.number, "state": pr.state, "head": {"ref": pr.head, "sha": pr.head_sha},
                           "base": {"ref": pr.base}, "title": pr.title, "body": pr.body,
                           "merged_at": pr.merged_at, "html_url": pr.url})

    monkeypatch.setattr(case.tracker, "get_pr", GhTracker("owner/repo", run=api).get_pr)
    listing = case.tracker.list_prs

    def no_closed_scan(state="open", head_prefix=""):
        assert state != "merged", "Owner downloaded the full closed-PR history"
        return listing(state, head_prefix)

    monkeypatch.setattr(case.tracker, "list_prs", no_closed_scan)
    gate(case)
    assert 4 <= len(calls) <= 8  # Includes the actual merged fact after publication.
    assert len(case.tracker.merged_prs) == 1
    assert git_command(case.history.origin, "show", "main:src/member_5.py") == "VALUE = 5"


def test_restart_reaps_durable_owner_checks_before_launching_more(proto, monkeypatch, fake_process_table):
    from foragent.issue import parse_body
    from process_fakes import install_popen

    case, table = proto, fake_process_table
    queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 22\n"})
    path = case.store.root / "merge-owner/5.json"
    checkpoints = []

    def during_check(process):
        checkpoint = path.read_bytes()
        identities = json.loads(checkpoint)["process_identities"]
        assert {item["pid"] for item in identities} >= {42000, 42001}
        if checkpoints:
            assert {target for kind, target, sig in table.signals} >= {42000, 42001}
        checkpoints.append(checkpoint)

    command = parse_body(case.store.load_approval(5).body).done_when_commands[0]
    install_popen(monkeypatch, table, matches=lambda argv: argv == ["/bin/sh", "-c", command],
                  children=(42001,), communicate=during_check)
    gate(case)
    assert checkpoints
    # Restore precisely the durable state at a hypothetical SIGKILL. No actual
    # process is killed; only the fake process table retains the orphan tree.
    path.write_bytes(checkpoints[0])
    # The checkpoint precedes the refresh push, so restore the offline remote
    # too; a kill at that moment could not have published the later head.
    previous_head = json.loads(checkpoints[0])["head"]
    pr = pr_for(case, 5)
    git_command(case.history.origin, "update-ref", f"refs/heads/{pr.head}", previous_head)
    case.tracker.set_pr_head_sha(pr.number, previous_head)
    table.add(42000, cwd=case.root, start_time=10)
    table.add(42001, ppid=1, cwd=case.root, start_time=11)
    gate(case)
    assert len(checkpoints) >= 2
    assert {target for kind, target, sig in table.signals} == {42000, 42001}
    gate(case)
    assert len(case.tracker.merged_prs) == 1


def test_cleanup_does_not_kill_reused_pid_or_depend_on_github(proto, monkeypatch, fake_process_table):
    case, table = proto, fake_process_table
    queue_workers(case, [5])
    row = next(iter(merge_owner.admissions(case.store).values()))
    path = case.store.root / "merge-owner/5.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"started_at": row["started_at"], **row["publication"],
                                "process_identities": [{"pid": 42000, "start_time": 10},
                                                       {"pid": 42001, "start_time": 11}]}))
    table.add(42000, cwd=case.root, start_time=99)  # PID reused by somebody else.
    table.add(42001, cwd=case.root, start_time=11)
    case.store.pause_github(case.clock[0] + timedelta(hours=1))
    gate(case)
    assert table.signals and {target for kind, target, sig in table.signals} == {42001}
    assert 42000 in table.live
    assert json.loads(path.read_text())["process_identities"] == []
    assert merge_owner.pending(case.store, 5)


def test_refused_process_cleanup_defers_only_its_admission(proto, monkeypatch, fake_process_table):
    from foragent import processes

    case, table = proto, fake_process_table
    queue_workers(case, [5, 1])
    row = merge_owner.admissions(case.store)[(5, case.store.get_run(5).started_at)]
    path = case.store.root / "merge-owner/5.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"started_at": row["started_at"], **row["publication"],
                                "process_identities": [{"pid": 42000, "start_time": 10}]}))
    table.add(42000, cwd=case.root, start_time=10)

    def denied(pid, sig):
        if pid == 42000:
            raise PermissionError("Cannot stop this recorded process")
        return table.kill(pid, sig)

    monkeypatch.setattr(processes.os, "kill", denied)
    report = gate(case, start_runs=True)
    assert any("cleanup deferred" in action for action in report.actions)
    assert merge_owner.pending(case.store, 5) and 42000 in table.live
    assert case.tracker.get_issue(1).state == "closed"
    assert git_command(case.history.origin, "show", "main:src/member_1.py") == "VALUE = 1"
    assert set(case.launches) & {2, 4, 77}


@pytest.mark.parametrize("deleted", [True, False])
def test_missing_issue_retires_only_that_admission_and_transport_retries(proto, monkeypatch, deleted):
    case = proto
    queue_workers(case, [5, 1])
    get_issue = case.tracker.get_issue
    if deleted:
        case.tracker.issues.pop(5)

    def unavailable(n):
        if n == 5:
            raise TrackerError("issue read: HTTP 404" if deleted else "connection reset by peer")
        return get_issue(n)

    monkeypatch.setattr(case.tracker, "get_issue", unavailable)
    gate(case, start_runs=True)
    assert merge_owner.pending(case.store, 5) is not deleted
    assert case.tracker.get_issue(1).state == "closed"
    assert set(case.launches) & {2, 4, 77} and 5 not in case.launches
    assert git_command(case.history.origin, "show", "main:src/member_1.py") == "VALUE = 1"
    if not deleted:
        monkeypatch.setattr(case.tracker, "get_issue", get_issue)
        drain(case, count=1)
        assert case.tracker.get_issue(5).state == "closed"


@pytest.mark.parametrize("boundary", ["reconciliation", "advancement", "repair", "completion"])
def test_one_admission_failure_does_not_block_other_merges_or_scheduler(proto, monkeypatch, boundary):
    case = proto
    queue_workers(case, [5, 1])
    pr = pr_for(case, 5)
    if boundary == "completion":
        case.tracker.merge_pr(pr.number, expected_head_sha=case.tracker.pr_head_sha(pr.number))
        method, number = "close_issue", 5
    elif boundary == "repair":
        case.tracker.set_pr_checks(pr.number, "fail")
        method, number = "close_pr", pr.number
    elif boundary == "advancement":
        method, number = "pr_checks", pr.number
    else:
        method, number = "get_pr", pr.number
    original = getattr(case.tracker, method)

    def unavailable(n, *args, **kwargs):
        if n == number:
            raise TrackerError("connection reset by peer")
        return original(n, *args, **kwargs)

    monkeypatch.setattr(case.tracker, method, unavailable)
    gate(case, start_runs=True)
    assert merge_owner.pending(case.store, 5)
    assert set(case.launches) & {2, 4, 77} and 5 not in case.launches
    # Completion of #5 moved main; #1 may need one clean refresh first.
    for _ in range(2):
        if case.tracker.get_issue(1).state == "closed":
            break
        gate(case)
    assert case.tracker.get_issue(1).state == "closed"
    assert git_command(case.history.origin, "show", "main:src/member_1.py") == "VALUE = 1"


@pytest.mark.parametrize("failure", ["ci", "head", "approval"])
def test_terminal_failure_yields_to_another_admission_in_the_same_tick(proto, monkeypatch, failure):
    case = proto
    # Local evidence keeps this about turn ownership when approval removal
    # changes CI's current exclusion scope for the other queued member.
    case.config = replace(case.config, checks="local")
    case.ctx.config = case.config
    monkeypatch.setattr(case.ci, "reports", lambda row: [])
    queue_workers(case, [5, 1])
    if failure == "ci":
        case.tracker.set_pr_checks(pr_for(case, 5).number, "fail")
    elif failure == "head":
        update_pr_branch(case, 5)
    else:
        case.tracker.remove_labels(5, ["approved"])
    gate(case)
    assert not merge_owner.pending(case.store, 5)
    assert case.tracker.get_issue(1).state == "closed"
    assert git_command(case.history.origin, "show", "main:src/member_1.py") == "VALUE = 1"


def test_slow_owner_check_leaves_scheduler_and_tick_lock_available(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 30\n"})
    entered, release = Event(), Event()
    verify = merge_owner._reverify
    futures = []

    def slow(*args):
        entered.set()
        assert release.wait(30), "Test did not release the fake slow check"
        return verify(*args)

    monkeypatch.setattr(merge_owner, "_reverify", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def launch(ctx, report):
            lock = ctx.store.lock_acceptance_baseline("merge-owner", blocking=False)
            if lock is not None:
                def owner():
                    with lock:
                        merge_owner.tick(ctx, report)
                futures.append(pool.submit(owner))

        monkeypatch.setattr(merge_owner, "start", launch)
        try:
            gate(case, start_runs=True)
            assert entered.wait(10)
            with case.store.tick_lock() as acquired:
                assert acquired
            assert set(case.launches) & {1, 2, 4, 77}
            assert case.store.lock_acceptance_baseline("merge-owner", blocking=False) is None
            gate(case, start_runs=True)
            assert len(futures) == 1, "Overlapping tick launched a second owner"
        finally:
            release.set()
        futures[0].result(timeout=60)


def test_process_launcher_transfers_owner_lock_without_waiting(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    children = []

    def spawn(argv, **options):
        assert argv[1:3] == ["-m", "foragent.merge_owner"]
        request = json.loads(argv[3])
        assert request["config"]["merge_owner"] is True
        assert request["store_root"] == str(case.store.root.resolve())
        fd = int(argv[4])
        assert options["pass_fds"] == (fd,) and options["start_new_session"]
        children.append(os.fdopen(os.dup(fd), "a"))  # Fake child's inherited descriptor.

    monkeypatch.setattr(merge_owner.subprocess, "Popen", spawn)
    report = SimpleNamespace(actions=[])
    try:
        case.launch_owner(case.ctx, report)
        assert len(children) == 1
        case.launch_owner(case.ctx, report)
        assert len(children) == 1
        assert case.store.lock_acceptance_baseline("merge-owner", blocking=False) is None
    finally:
        for child in children:
            child.close()
    lock = case.store.lock_acceptance_baseline("merge-owner", blocking=False)
    assert lock is not None
    lock.close()


def test_owner_process_bootstrap_releases_inherited_lock_during_github_backoff(proto):
    case = proto
    queue_workers(case, [5])
    case.store.pause_github(datetime.now(UTC) + timedelta(hours=1))
    request = {"repo": "owner/repo", "repo_root": str(case.root), "store_root": str(case.store.root),
               "config": asdict(case.config), "committed_config": False}
    lock = case.store.lock_acceptance_baseline("merge-owner", blocking=False)
    assert lock is not None
    try:
        child = subprocess.Popen(
            [sys.executable, "-m", "foragent.merge_owner", json.dumps(request), str(lock.fileno())],
            pass_fds=(lock.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    finally:
        lock.close()
    stdout, stderr = child.communicate()  # No timeout or signal to a real process.
    assert child.returncode == 0, stdout + stderr
    lock = case.store.lock_acceptance_baseline("merge-owner", blocking=False)
    assert lock is not None
    lock.close()
    assert merge_owner.pending(case.store, 5)


def test_repair_replays_labels_after_lost_successful_close(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    case.history.update({"src/member_5.py": "VALUE = 'conflict'\n"})
    close = case.tracker.close_pr
    calls = []

    def lost(pr, *args, **kwargs):
        calls.append(pr)
        close(pr, *args, **kwargs)
        raise TrackerError("Lost close response")

    monkeypatch.setattr(case.tracker, "close_pr", lost)
    gate(case)
    assert case.tracker.get_pr(pr_for(case, 5).number).state == "closed"
    assert "blocked" in case.tracker.get_issue(5).labels
    assert merge_owner.pending(case.store, 5)
    gate(case)
    assert len(calls) == 1, "A closed PR should finish the transition without another close"
    assert not merge_owner.pending(case.store, 5)
    assert "ready" in case.tracker.get_issue(5).labels
    assert "blocked" not in case.tracker.get_issue(5).labels
    assert retry_marker(case.store.root, 5).exists()


def test_repair_close_cannot_delete_a_concurrent_push(proto, monkeypatch):
    case = proto
    queue_workers(case, [5])
    case.tracker.set_pr_checks(pr_for(case, 5).number, "fail")
    close = case.tracker.close_pr
    pushed = []

    def changed_after_guard(pr, *args, **kwargs):
        assert kwargs["delete_branch"] is False
        pushed.append(update_pr_branch(case, 5))
        return close(pr, *args, **kwargs)

    monkeypatch.setattr(case.tracker, "close_pr", changed_after_guard)
    gate(case)
    assert pushed and not case.tracker.deleted_branches
    assert git_command(case.history.origin, "rev-parse", "foragent/issue-5") == pushed[-1]


def test_owner_gh_close_and_merge_preserve_branches():
    calls = []

    def transport(args, input=None):
        calls.append(args)
        return ""

    tracker = GhTracker("owner/repo", run=transport)
    tracker.close_pr(10, delete_branch=False)
    tracker.merge_pr(11, expected_head_sha="a" * 40, subject="Approved title", delete_branch=False)
    assert len(calls) == 2 and all("--delete-branch" not in call for call in calls)
    assert calls[0][:3] == ["pr", "close", "10"]
    assert calls[1][:3] == ["pr", "merge", "11"]


@pytest.mark.parametrize("change", ["target", "head"])
def test_manual_merge_must_match_admitted_publication(proto, monkeypatch, change):
    from foragent.run_baseline import unmerged_acceptance_owners

    case = proto
    queue_workers(case, [5])
    pr = pr_for(case, 5)
    main = git_command(case.history.origin, "rev-parse", "main")
    if change == "target":
        case.tracker.update_pr(pr.number, base="integration", title=pr.title, body=pr.body)
    else:
        update_pr_branch(case, 5)
    FakeTracker.merge_pr(case.tracker, pr.number, expected_head_sha=case.tracker.pr_head_sha(pr.number))
    installed = []
    monkeypatch.setattr(runner, "after_merge", lambda *args: installed.append(args))
    gate(case)
    assert case.tracker.get_issue(5).state == "open"
    assert "blocked" in case.tracker.get_issue(5).labels
    assert not installed and not contract.turns(case)
    assert not case.store.owner_merge_facts()
    assert any(5 in owners for owners in unmerged_acceptance_owners(case.ctx).values())
    assert git_command(case.history.origin, "rev-parse", "main") == main


def test_human_authorization_names_requested_pr_and_target(proto, monkeypatch):
    from foragent.orch import merge

    case = proto
    queue_workers(case, [5])
    main_pr = pr_for(case, 5)
    head = case.tracker.pr_head_sha(main_pr.number)
    case.config = replace(case.config, checks="local")
    case.ctx.config = case.config
    case.worker.number = 5
    requested = case.tracker.create_pr(head=main_pr.head, base="main", title="Requested PR", body="")
    case.tracker.set_pr_head_sha(requested, head)
    case.clock[0] += timedelta(seconds=1)
    assert "merge-queued" in merge(case.ctx, requested, approved_by="human")
    row = merge_owner.admissions(case.store)[(5, case.store.get_run(5).started_at)]
    assert row["publication"]["pr"] == requested
    assert row["publication"]["authorization"]["target"] == "main"
    drain(case, count=1)
    assert case.tracker.get_pr(main_pr.number).state == "open"
    assert case.tracker.get_pr(requested).merged_at


def test_admitted_crash_label_failure_does_not_abort_other_work(proto, monkeypatch):
    case = proto
    queue_workers(case, [5, 1])
    # Restore the running record from the admission generation: the worker died
    # before finish_run, after its durable handoff.
    path = case.store.root / "runs/5.json"
    value = json.loads(path.read_text())
    value.update(status="running", outcome=None, pid=None)
    path.write_text(json.dumps(value))
    case.tracker.add_labels(5, ["running"])
    case.clock[0] += timedelta(minutes=5)
    remove = case.tracker.remove_labels

    def unavailable(n, labels):
        if n == 5:
            raise TrackerError("Connection reset removing admitted worker labels")
        return remove(n, labels)

    monkeypatch.setattr(case.tracker, "remove_labels", unavailable)
    # This restored run has no PID. Supply confirmed absence through the public
    # liveness seam instead of depending on this machine's ps availability.
    report = gate(case, start_runs=True, runner_process_exists=lambda n: False)
    assert any("label cleanup deferred" in action for action in report.actions)
    assert case.store.get_run(5).status == "finished"
    assert merge_owner.pending(case.store, 5)
    assert case.tracker.get_issue(1).state == "closed"
    assert set(case.launches) & {2, 4, 77}
    monkeypatch.setattr(case.tracker, "remove_labels", remove)
    drain(case, count=1)
    assert case.tracker.get_issue(5).state == "closed"


def test_owner_merge_fact_drives_scorecard_and_ownership(proto):
    from foragent import orch_scorecard, orch_scorecard_records, run_baseline

    case = proto
    queue_workers(case, [5])
    drain(case, count=1)
    run = case.store.get_run(5)
    assert run.outcome == "merge-queued"
    fact = case.store.owner_merge_facts()[5, run.started_at]
    pr = case.tracker.get_pr(fact["pr"])
    assert fact == {"pr": pr.number, "target": pr.base, "head": pr.head_sha, "merged_at": pr.merged_at}
    # world also has an earlier fixture-unused execution of #5. Inspect the
    # admitted execution without treating that unrelated history row as a merge.
    issues = orch_scorecard_records._issues(case.store, case.config.lanes)
    rows = [row for _, row in orch_scorecard._runs(case.store, case.clock[0], 7, issues)
            if row["number"] == 5 and row["outcome"] == run.outcome]
    assert len(rows) == 1 and rows[0]["merged"] and not rows[0]["failed"]
    executions = [row for row in orch_scorecard_records._executions(case.store, case.clock[0])
                  if row["number"] == 5 and row["started"].isoformat() == run.started_at]
    assert len(executions) == 1 and executions[0]["merged"]
    assert not any(5 in owners for owners in run_baseline.unmerged_acceptance_owners(case.ctx).values())


def test_flag_off_ci_default_branch_failure_keeps_original_fallback(proto, monkeypatch):
    from foragent import ci_local

    case = proto
    case.config = replace(case.config, merge_owner=False, checks_fallback="local")
    case.ctx.config = case.config
    fallback = ci_local.fallback_comparison
    measured = []

    def local(*args, **kwargs):
        measured.append(True)
        return fallback(*args, **kwargs)

    def unavailable():
        raise TrackerError("HTTP 502 reading default branch")

    monkeypatch.setattr(ci_local, "fallback_comparison", local)
    monkeypatch.setattr(case.tracker, "pr_default_branch", unavailable)
    monkeypatch.setattr(case.tracker, "get_pr", lambda *args: pytest.fail("Flag-off path used owner PR reads"))
    case.worker.number = 5
    case.clock[0] += timedelta(seconds=1)
    case.store.start_run(5, branch="foragent/issue-5", worktree=case.store.worktree_path(5))
    case.tracker.add_labels(5, ["running"])
    assert runner.run_issue(5, case.ctx) == "merged", case.store.log_path(5).read_text()
    assert measured and not merge_owner.admissions(case.store)
    assert not case.store.owner_merge_facts()


def test_completed_failed_acceptance_producer_is_terminal(proto, monkeypatch):
    from foragent.ci_workflow import WORKFLOW
    from foragent.merge_owner_reads import Snapshot

    case = proto
    queue_workers(case, [5])
    calls = []

    def api(args, input=None):
        calls.append(args)
        assert "/actions/runs?head_sha=" in args[-1]
        head = args[-1].split("head_sha=", 1)[1].split("&", 1)[0]
        return json.dumps([{"id": 123, "head_sha": head, "path": WORKFLOW, "event": "pull_request",
                            "status": "completed", "conclusion": "failure"}])

    reports = Snapshot(GhTracker("owner/repo", run=api)).tracker.pr_acceptance_reports
    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", reports)
    gate(case)
    assert not merge_owner.pending(case.store, 5)
    assert "blocked" in case.tracker.get_issue(5).labels
    assert not case.tracker.merged_prs
    reads = len(calls)
    gate(case)
    assert len(calls) == reads, "A rejected producer stayed in the polling queue"


def test_forty_pending_admissions_read_each_github_fact_once(proto, monkeypatch):
    from collections import Counter
    from foragent import run_merge_turn

    case = proto
    queue_workers(case, [5])
    source = next(iter(merge_owner.admissions(case.store).values()))
    publication = source["publication"]
    numbers = [5, *range(100, 139)]
    # Distinct, real commit objects with the admitted tree. This needs no model
    # work or repeated verification to measure forty pending publications.
    git_command(case.history.origin, "fast-import", "--quiet", input="".join(
        f"commit refs/heads/foragent/issue-{n}\ncommitter Offline <tests@example.com> 1792483200 +0000\n"
        f"data {len(str(n))}\n{n}\nfrom {publication['head']}\n\n" for n in numbers[1:]) + "done\n")
    for n in numbers[1:]:
        case.worker.number = n
        case.tracker.add_issue(n, f"Pending #{n}", publication["approval"]["body"], labels=["approved"])
        approval = {**publication["approval"], "number": n}
        case.store.save_approval(**approval)
        branch = f"foragent/issue-{n}"
        head = git_command(case.history.origin, "rev-parse", branch)
        pr = case.tracker.create_pr(head=branch, base="main", title=f"Pending #{n}", body="")
        case.store.start_run(n, branch=branch, worktree=case.store.worktree_path(n))
        place = run_merge_turn.seat(case.ctx, n, "main")
        run_merge_turn.join(case.ctx, place, head, merge_owner=True, publication={
            **publication, "pr": pr, "branch": branch, "head": head, "approval": approval, "comparison": None})
        case.store.finish_run(n, outcome="merge-queued")

    original = case.tracker
    calls = []

    def api(args, input=None):
        calls.append(tuple(args))
        if args[:2] == ["pr", "checks"]:
            pr = int(args[2])
            return json.dumps([{"bucket": "pending", "name": "ordinary", "workflow": "build",
                                "link": f"https://github.com/owner/repo/actions/runs/{pr}/job/1"}])
        path = args[-1]
        prefix = "repos/owner/repo/"
        path = path.removeprefix(prefix)
        if path.startswith("pulls/"):
            pr = int(path.split("/")[1])
            if path.endswith("/files?per_page=100"):
                return json.dumps([{"filename": "src/member_5.py"}])
            row = original.get_pr(pr)
            return json.dumps({"number": pr, "head": {"ref": row.head, "sha": row.head_sha},
                               "base": {"ref": row.base}, "title": row.title, "body": row.body,
                               "state": "open", "html_url": row.url, "merged_at": None})
        if path.startswith("issues/"):
            if path.endswith("/parent"):
                raise TrackerError("HTTP 404 (no parent)")
            issue = original.get_issue(int(path.split("/")[1]))
            return json.dumps({"number": issue.number, "title": issue.title, "body": issue.body,
                               "state": "open", "labels": [{"name": label} for label in issue.labels]})
        pytest.fail(f"Unexpected GitHub read: {args}")

    def launch(ctx, report):
        case.owner_starts.append(replace(ctx, tracker=GhTracker("owner/repo", run=api)))

    monkeypatch.setattr(merge_owner, "start", launch)
    gate(case)
    assert len(merge_owner.admissions(case.store)) == 40
    assert not original.merged_prs
    # Current main no longer excludes acceptance check runs by workflow provenance
    # in pr_checks, and batch review no longer reads the issue's parent: four reads
    # per pending PR, with no final locked recheck.
    assert len(calls) == 160
    assert not [args for args in calls if args[-1].endswith("/parent")]
    assert max(Counter(calls).values()) == 1
    for n in numbers:
        log = case.store.log_path(n)
        assert not log.exists() or "Merge owner deferred:" not in log.read_text()
    ci_calls = [args for args in calls if args[:2] == ("pr", "checks")
                or "/files?" in args[-1] or "/check-runs?" in args[-1] or "/actions/runs?" in args[-1]]
    assert len(ci_calls) == 80
    print("40 pending admissions: 160 gh calls total, 80 CI-related, no duplicate reads")
