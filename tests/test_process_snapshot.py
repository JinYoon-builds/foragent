"""Tick snapshot regressions use only fake identities and fake signals."""

from dataclasses import replace
from datetime import timedelta
import json
import subprocess
from types import SimpleNamespace

import pytest

from foragent import orch_tick_outcome, processes, scheduler
from foragent.config import Config
from foragent.processes import list_processes as kernel_listing, read_process_identity as kernel_identity
from foragent.scheduler_stop import _run_processes_exist, _stop_run_processes, tick_processes
from foragent.state import StateStore
from foragent.tracker import FakeTracker


@pytest.fixture(autouse=True)
def snapshot_identity_io(fake_process_table, monkeypatch):
    def read_identity(pid):
        try:
            return fake_process_table.process(pid)
        except ProcessLookupError:
            return None

    monkeypatch.setattr(processes, "read_process_identity", read_identity)


@pytest.mark.parametrize("finished", [False, True])
def test_pruning_preserves_unreadable_pids_and_unknown_record_fields(
    fake_process_table, tmp_path, monkeypatch, finished,
):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    child = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=11)
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree=tmp_path, pid=root.pid)
    if finished:
        store.finish_run(1, "killed")
        path = store.root / "runs.jsonl"
        record = json.loads(path.read_text())
        record["future_field"] = {"keep": True}
        # Duplicate completion entries must both lose stale identities.
        path.write_text((json.dumps(record) + "\n") * 2)
    path = store.root / "runs/1.json"
    record = json.loads(path.read_text())
    record["future_field"] = {"keep": True}
    path.write_text(json.dumps(record))
    table.exit(child.pid)
    listing = processes.ProcessTable((row for row in table.list_processes() if row.pid != root.pid), {root.pid})
    monkeypatch.setattr(processes, "list_processes", lambda: listing)
    read = processes.read_process_identity

    def denied(pid):
        if pid == root.pid:
            raise PermissionError(pid)
        return read(pid)

    monkeypatch.setattr(processes, "read_process_identity", denied)
    with tick_processes(store):
        pass
    expected = [{"pid": root.pid, "start_time": root.start_time}]
    assert store.get_run(1).process_identities == expected
    assert json.loads(path.read_text())["future_field"] == {"keep": True}
    if finished:
        for line in (store.root / "runs.jsonl").read_text().splitlines():
            record = json.loads(line)
            assert record["process_identities"] == expected
            assert record["future_field"] == {"keep": True}
    assert not table.signals


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_unavailable_snapshot_is_cached_warns_and_preserves_all_records(
    fake_process_table, tmp_path, monkeypatch, caplog, error,
):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree=tmp_path, pid=root.pid)
    before = store.get_run(1)
    calls = []

    def unavailable():
        calls.append(True)
        raise error("fake table unavailable")

    monkeypatch.setattr(processes, "list_processes", unavailable)
    monkeypatch.setattr(store, "prune_process_identities", lambda **kwargs: pytest.fail("Unknown ownership cannot be pruned"))
    with tick_processes(store):
        assert processes.current_snapshot().available is False
    assert store.get_run(1) == before
    assert len(calls) == 1
    assert "skipping prune" in caplog.text
    assert not table.signals


def test_snapshot_revalidates_a_parent_before_attesting_new_children(fake_process_table, tmp_path, monkeypatch):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    child = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=21)
    listing = table.list_processes()
    table.rows[root.pid] = replace(root, start_time=20)
    monkeypatch.setattr(processes, "list_processes", lambda: listing)
    with processes.process_snapshot():
        observed = processes.observe_routine_tree([{"pid": root.pid, "start_time": root.start_time}])
    assert observed == [{"pid": root.pid, "start_time": root.start_time}]
    assert child.pid not in {item["pid"] for item in observed}
    assert not table.signals


@pytest.mark.parametrize("error", [PermissionError, OSError])
@pytest.mark.parametrize("finished", [False, True])
def test_unreadable_table_still_allows_a_successful_tick(
    fake_process_table, tmp_path, monkeypatch, caplog, error, finished,
):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    child = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=11)
    store = StateStore(tmp_path / "state")
    run = store.start_run(1, branch="branch", worktree=tmp_path, pid=root.pid)
    tracker = FakeTracker()
    tracker.add_issue(1, "Active run", labels=["running"])
    if finished:
        run = store.finish_run(1, "killed")
        tracker.remove_labels(1, ["running"])
        tracker.close_issue(1)
    before = store.read_history()
    table.exit(child.pid)
    calls = []
    removed = []
    fetched = []

    def unavailable():
        calls.append(True)
        raise error("fake unreadable table")

    monkeypatch.setattr(processes, "list_processes", unavailable)
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0))
    ctx = scheduler.TickContext(
        repo="owner/repo", repo_root=tmp_path, store=store, tracker=tracker,
        config=Config(), git=SimpleNamespace(fetch=lambda *args: fetched.append(args), remove_worktree=removed.append),
        now=store._now,
        launcher=lambda number: pytest.fail("Unexpected launch"), start_runs=False,
    )
    report = orch_tick_outcome.run_recorded(store, ctx.config, ctx.repo, lambda: scheduler.tick(ctx))
    assert report.skipped is None and not report.actions
    assert fetched, "The tick must continue doing work despite unavailable process evidence"
    assert orch_tick_outcome.read(store)["status"] == "ok"
    assert store.get_run(1) == run
    assert store.read_history() == before
    assert len(calls) == 1 and "skipping prune" in caplog.text
    assert len([record for record in caplog.records if record.name == "foragent.processes"]) == 1
    assert root.pid in table.live
    assert not removed and not table.signals
    monkeypatch.setattr(processes, "list_processes", table.list_processes)
    assert scheduler.tick(ctx).skipped is None
    assert child.pid not in {item["pid"] for item in store.get_run(1).process_identities}
    assert (root.pid in table.live) is not finished


@pytest.mark.parametrize("command,expected", [
    (("fa", "gate", "run-issue", "1"), True),
    (("fa", "gate", "run-issue", "11"), False),
    (("fa", "gate", "status"), False),
    (None, False),
])
def test_pidless_runner_discovery_uses_snapshot_without_ps(
    fake_process_table, tmp_path, monkeypatch, command, expected,
):
    table = fake_process_table
    rows = [processes.ProcessIdentity(row.pid, row.ppid, row.pgid, row.sid, row.start_time)
            for row in table.list_processes()]
    rows.append(processes.ProcessIdentity(42000, 1, 42000, 42000, 10, command, processes.os.getuid()))
    listings = []
    monkeypatch.setattr(processes, "list_processes", lambda: listings.append(True) or tuple(rows))
    probes = []

    def ps(*args, **kwargs):
        probes.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="  PID COMMAND\n")

    monkeypatch.setattr(scheduler.subprocess, "run", ps)
    with processes.process_snapshot():
        assert scheduler.runner_process_exists(1) is expected
        assert scheduler.runner_process_exists(1) is expected
    assert len(listings) == 1
    assert len(probes) == (0 if expected else 2), "Only a positive snapshot match can replace fresh discovery"


def test_launch_inside_tick_keeps_its_separate_birth_evidence(fake_process_table, tmp_path):
    table = fake_process_table
    store = StateStore(tmp_path / "state")
    table.list_calls = 0
    with tick_processes(store):
        root = table.add(42000, cwd=tmp_path, start_time=10)
        store.start_run(1, branch="branch", worktree=tmp_path, pid=root.pid)
        store.update_run(1, phase="implement")
    assert store.get_run(1).process_identities == [{"pid": root.pid, "start_time": root.start_time}]
    assert table.list_calls == 5, "Launch and state updates retain their original two fresh listings"
    assert not table.signals


def test_birth_recorded_by_another_process_after_snapshot_is_not_pruned(fake_process_table, tmp_path):
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree=tmp_path)
    with tick_processes(store):
        snapshot = processes.current_snapshot()
        # A runner can write birth evidence after the gate's enumeration.
        identity = {"pid": 42000, "start_time": snapshot.captured_at + 1}
        fake_process_table.add(42000, cwd=tmp_path, start_time=identity["start_time"])
        store._write_run(replace(store.get_run(1), pid=42000, process_identities=[identity]))
        store.update_run(1, phase="implement")
    assert store.get_run(1).process_identities == [identity]
    assert not fake_process_table.signals


@pytest.mark.parametrize("finished", [False, True])
@pytest.mark.parametrize("identity_role", ["runner", "descendant"])
@pytest.mark.parametrize("evidence", [
    "rounded_birth", "cached_iterator", "stale_snapshot", "gone", "reused",
    "denied", "psutil_denied", "read_error",
])
def test_pruning_rechecks_absent_recorded_identities_before_removal(
    fake_process_table, tmp_path, monkeypatch, finished, identity_role, evidence,
):
    table = fake_process_table
    pid, start = 42000, 20 if evidence in {"cached_iterator", "stale_snapshot"} else 100.0
    if evidence != "rounded_birth":
        table.add(pid, cwd=tmp_path, start_time=10 if evidence == "stale_snapshot" else start)
    store = StateStore(tmp_path / "state")
    run = store.start_run(1, branch="branch", worktree=tmp_path)
    identity = {"pid": pid, "start_time": start}
    reads = []

    def process(target, *, cached_start=None):
        if evidence == "denied":
            raise PermissionError(target)
        if evidence == "psutil_denied":
            raise processes.psutil.AccessDenied(target)
        if evidence == "read_error":
            raise OSError("fake identity read failed")
        row = table.process(target)
        return SimpleNamespace(
            pid=target, create_time=lambda: row.start_time if cached_start is None else cached_start,
            ppid=lambda: row.ppid, status=lambda: "running", cmdline=lambda: [],
            uids=lambda: SimpleNamespace(real=processes.os.getuid()),
        )

    def read_identity(target):
        reads.append(target)
        return kernel_identity(target)

    monkeypatch.setattr(processes.psutil, "Process", process)
    monkeypatch.setattr(processes.psutil, "process_iter", lambda: iter(
        [process(pid, cached_start=10)] if evidence in {"cached_iterator", "stale_snapshot"} else []
    ))
    monkeypatch.setattr(processes, "list_processes", kernel_listing)
    monkeypatch.setattr(processes, "read_process_identity", read_identity)
    monkeypatch.setattr(processes.time, "time", lambda: 100.001)
    with tick_processes(store):
        snapshot = processes.current_snapshot()
        assert pid not in snapshot.rows or snapshot.rows[pid].start_time != start
        if evidence == "rounded_birth":
            # Birth is published after enumeration; the kernel rounds it below captured_at.
            table.add(pid, cwd=tmp_path, start_time=start)
        elif evidence == "stale_snapshot":
            # A new recorded lifetime can also replace a PID still in the snapshot.
            table.add(pid, cwd=tmp_path, start_time=start)
        elif evidence == "gone":
            table.exit(pid)
        elif evidence == "reused":
            table.add(pid, cwd=tmp_path, start_time=start + 1)
        record = store._write_run(replace(
            run, pid=pid if identity_role == "runner" else None,
            process_identities=[identity], status="finished" if finished else "running",
        ))
        if finished:
            (store.root / "runs.jsonl").write_text(json.dumps(record) + "\n")
        else:
            store.update_run(1, phase="implement")
    retained_runner = identity_role == "runner" and not finished
    expected = [] if evidence in {"gone", "reused"} and not retained_runner else [identity]
    assert store.get_run(1).process_identities == expected
    if finished:
        assert store.read_history()[0].process_identities == expected
    if not retained_runner:
        assert reads and set(reads) == {pid}, "Snapshot absence must be checked with per-PID evidence"
    assert not table.signals


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("unreadable", ["identity", "argv"])
def test_pidless_recovery_scopes_unreadable_processes_to_current_user(
    fake_process_table, tmp_path, monkeypatch, owned, unreadable,
):
    table = fake_process_table
    row = table.add(50000, cwd=tmp_path, start_time=10)
    uid = processes.os.getuid() + (0 if owned else 1)

    def create_time():
        if unreadable == "identity":
            raise processes.psutil.AccessDenied(row.pid)
        return row.start_time

    def cmdline():
        raise processes.psutil.AccessDenied(row.pid)

    process = SimpleNamespace(
        pid=row.pid, create_time=create_time, ppid=lambda: row.ppid, status=lambda: "running",
        cmdline=cmdline, uids=lambda: SimpleNamespace(real=uid),
    )
    monkeypatch.setattr(processes.psutil, "Process", lambda pid: process)
    monkeypatch.setattr(processes.psutil, "process_iter", lambda: iter([process]))
    monkeypatch.setattr(processes, "list_processes", kernel_listing)
    probes = []

    def ps(*args, **kwargs):
        probes.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="  PID COMMAND\n")

    monkeypatch.setattr(scheduler.subprocess, "run", ps)
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree=tmp_path / "missing-worktree")
    now = store._now() + scheduler.LAUNCH_GRACE + timedelta(seconds=1)
    tracker = FakeTracker()
    tracker.add_issue(1, "Dead pid-less run", labels=["running"])
    ctx = scheduler.TickContext(
        repo="owner/repo", repo_root=tmp_path, store=store, tracker=tracker, config=Config(),
        git=SimpleNamespace(remove_worktree=lambda path: None), now=lambda: now,
        launcher=lambda number: pytest.fail("Unexpected launch"), start_runs=False,
    )
    for _ in range(2):
        with tick_processes(store):
            snapshot = processes.current_snapshot()
            if unreadable == "identity":
                assert snapshot.unreadable == frozenset({50000})
            else:
                assert snapshot.rows[50000].argv is None
            assert scheduler.runner_process_exists(1) is False
            scheduler._recover_runs(ctx, scheduler.TickReport())
        assert not store.active_runs()
    assert store.get_run(1).outcome == "crashed"
    assert probes, "Unreadable unrelated processes require fresh run-specific discovery"
    assert not table.signals


def recovery_context(tmp_path, *, now=None):
    store = StateStore(tmp_path / "state")
    tracker = FakeTracker()
    removed = []
    ctx = scheduler.TickContext(
        repo="owner/repo", repo_root=tmp_path, store=store, tracker=tracker, config=Config(),
        git=SimpleNamespace(remove_worktree=removed.append), now=now or store._now,
        launcher=lambda number: pytest.fail("Unexpected launch"), start_runs=False,
    )
    return ctx, removed


@pytest.mark.parametrize("evidence", ["rounded_birth", "cached_iterator", "stale_snapshot", "denied"])
def test_recovery_confirms_absent_runner_with_fresh_identity(
    fake_process_table, tmp_path, monkeypatch, evidence,
):
    table = fake_process_table
    ctx, removed = recovery_context(tmp_path)
    ctx.tracker.add_issue(1, "Healthy runner", labels=["running"])
    run = ctx.store.start_run(1, branch="branch", worktree=tmp_path / "worktree")
    if evidence == "stale_snapshot":
        table.add(42000, cwd=tmp_path, start_time=90)
    elif evidence == "cached_iterator":
        table.add(42000, cwd=tmp_path, start_time=100.0)
        listing = tuple(row for row in table.list_processes() if row.pid != 42000)
        listings = iter((listing,))
        monkeypatch.setattr(processes, "list_processes", lambda: next(listings, None) or table.list_processes())
    monkeypatch.setattr(processes.time, "time", lambda: 100.001)
    with tick_processes(ctx.store):
        root = table.add(42000, cwd=tmp_path, start_time=100.0)
        identity = {"pid": root.pid, "start_time": root.start_time}
        ctx.store._write_run(replace(run, pid=root.pid, process_identities=[identity]))
        if evidence == "denied":
            def denied(pid):
                raise PermissionError(pid)
            monkeypatch.setattr(processes, "read_process_identity", denied)
        report = scheduler.TickReport()
        scheduler._recover_runs(ctx, report)
        assert ctx.store.get_run(1).status == "running"
        assert ctx.store.get_run(1).process_identities == [identity]
        assert not report.actions
    assert root.pid in table.live
    assert not removed and not table.signals and not ctx.store.read_history()


@pytest.mark.parametrize("unreadable", [False, True])
def test_shared_stop_checks_recorded_executor_born_after_listing(
    fake_process_table, tmp_path, monkeypatch, unreadable,
):
    table = fake_process_table
    ctx, removed = recovery_context(tmp_path)
    for number, pid in ((1, 42000), (2, 43000)):
        ctx.tracker.add_issue(number, f"Run {number}", labels=["running"])
        table.add(pid, cwd=tmp_path, start_time=10)
        ctx.store.start_run(number, branch=f"branch-{number}", worktree=tmp_path, pid=pid)
    with tick_processes(ctx.store):
        assert _stop_run_processes(ctx, ctx.store.get_run(1), scheduler.TickReport()) == "stopped"
        child = table.add(43001, ppid=43000, cwd=tmp_path, start_time=11)
        ctx.store.update_run(2, executor_pgid=child.pid, process_identities=[
            {"pid": child.pid, "start_time": child.start_time},
        ])
        if unreadable:
            read = processes.read_process_identity
            def denied(pid):
                if pid == child.pid:
                    raise PermissionError(pid)
                return read(pid)
            monkeypatch.setattr(processes, "read_process_identity", denied)
        result = _stop_run_processes(ctx, ctx.store.get_run(2), scheduler.TickReport())
        assert result == ("refused" if unreadable else "stopped")
        assert (child.pid in table.live) is unreadable
        assert ctx.store.get_run(2).executor_pgid == (child.pid if unreadable else None)
    assert table.list_calls > 0
    assert not removed


@pytest.mark.parametrize("unreadable", [False, True])
def test_refused_stop_checks_recorded_child_absent_from_routine_snapshot(
    fake_process_table, tmp_path, monkeypatch, unreadable,
):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    ctx, removed = recovery_context(tmp_path)
    ctx.tracker.add_issue(1, "Refused stop", labels=["running"])
    run = ctx.store.start_run(1, branch="branch", worktree=tmp_path / "worktree", pid=root.pid)
    scheduler.flag(ctx, 1, scheduler.Reason.STOP_REFUSED, "Earlier stop refused")
    # A cached iterator can omit the runner too, making recovery reach its dead
    # branch in this same tick after the runner publishes the child's birth.
    listing = tuple(row for row in table.list_processes() if row.pid != root.pid)
    listings = iter((listing,))
    monkeypatch.setattr(processes, "list_processes", lambda: next(listings, None) or table.list_processes())
    with tick_processes(ctx.store):
        child = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=11)
        ctx.store.update_run(1, process_identities=[{"pid": child.pid, "start_time": child.start_time}])
        table.exit(root.pid)
        if unreadable:
            read = processes.read_process_identity
            def denied(pid):
                if pid == child.pid:
                    raise OSError("fake unreadable child")
                return read(pid)
            monkeypatch.setattr(processes, "read_process_identity", denied)
        run = ctx.store.get_run(1)
        assert _run_processes_exist(run)
        assert _stop_run_processes(ctx, run, scheduler.TickReport()) == "refused"
        scheduler._recover_runs(ctx, scheduler.TickReport())
        assert ctx.store.get_run(1).status == "running"
    assert child.pid in table.live
    assert not removed and not table.signals and not ctx.store.read_history()


@pytest.mark.parametrize("evidence", ["command", "late_command", "grace", "unrelated", "unavailable"])
def test_pidless_recovery_uses_only_run_specific_evidence(
    fake_process_table, tmp_path, monkeypatch, evidence,
):
    table = fake_process_table
    ctx, removed = recovery_context(tmp_path)
    ctx.tracker.add_issue(1, "Pid-less run", labels=["running"])
    run = ctx.store.start_run(1, branch="branch", worktree=tmp_path / "worktree")
    elapsed = scheduler.LAUNCH_GRACE + timedelta(seconds=-1 if evidence == "grace" else 1)
    ctx.now = lambda: scheduler.datetime.fromisoformat(run.started_at) + elapsed
    row = table.add(50000, cwd=tmp_path, start_time=10)
    commands = ("fa", "gate", "run-issue", "1") if evidence == "command" else None
    listing = processes.ProcessTable(
        [processes.ProcessIdentity(row.pid, row.ppid, row.pgid, row.sid, row.start_time,
                                   commands, processes.os.getuid())] if evidence == "command" else [],
        unreadable=[row.pid], unreadable_uids={row.pid: processes.os.getuid()},
    )
    monkeypatch.setattr(processes, "list_processes", lambda: listing)
    def ps(*args, **kwargs):
        if evidence == "unavailable":
            raise PermissionError("fake unavailable discovery")
        command = "50000 fa gate run-issue 1\n" if evidence == "late_command" else ""
        return subprocess.CompletedProcess(args, 0, stdout="  PID COMMAND\n" + command)

    monkeypatch.setattr(scheduler.subprocess, "run", ps)
    with tick_processes(ctx.store):
        scheduler._recover_runs(ctx, scheduler.TickReport())
    dead = evidence == "unrelated"
    assert (ctx.store.get_run(1).status == "running") is not dead
    assert bool(removed) is dead
    assert row.pid in table.live and not table.signals


@pytest.mark.parametrize("cached", ["matching", "absent", "mismatched"])
@pytest.mark.parametrize("current", ["alive", "gone", "reused", "error"])
@pytest.mark.parametrize("fresh", [False, True])
def test_recorded_identity_evidence_distinguishes_cached_absence_from_death(
    fake_process_table, tmp_path, monkeypatch, cached, current, fresh,
):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    rows = {} if cached == "absent" else {
        root.pid: replace(root, start_time=5) if cached == "mismatched" else root,
    }
    if current == "gone":
        table.exit(root.pid)
    elif current == "reused":
        table.add(root.pid, cwd=tmp_path, start_time=20)
    read = processes.read_process_identity
    reads = []

    def read_identity(pid):
        reads.append(pid)
        if current == "error":
            raise RuntimeError("fake identity read failed")
        return read(pid)

    monkeypatch.setattr(processes, "read_process_identity", read_identity)
    evidence = processes.recorded_identity_evidence(root.pid, root.start_time, rows=rows, fresh=fresh)
    cached_match = cached == "matching" and not fresh
    expected = True if cached_match or current == "alive" else None if current == "error" else False
    assert evidence.alive is expected
    assert bool(reads) is not cached_match
    assert (evidence.row is not None) is (expected is True)
    assert not table.signals


@pytest.mark.parametrize("replacement", [False, True])
def test_recovery_keeps_death_evidence_after_pruning_the_runner_record(
    fake_process_table, tmp_path, replacement,
):
    table = fake_process_table
    ctx, removed = recovery_context(tmp_path)
    ctx.tracker.add_issue(1, "Dead runner", labels=["running"])
    root = table.add(42000, cwd=tmp_path, start_time=10)
    ctx.store.start_run(1, branch="branch", worktree=tmp_path / "worktree", pid=root.pid)
    table.exit(root.pid)
    if replacement:
        table.add(root.pid, cwd=tmp_path, start_time=20)
    with tick_processes(ctx.store):
        scheduler._recover_runs(ctx, scheduler.TickReport())
    assert ctx.store.get_run(1).status == "finished"
    assert ctx.store.get_run(1).outcome == "crashed"
    assert bool(removed)
    assert (root.pid in table.live) is replacement
    assert not table.signals


@pytest.mark.parametrize("reuse_before_prune", [False, True])
def test_recovery_preserves_birth_of_runner_launched_and_crashed_inside_tick(
    fake_process_table, tmp_path, reuse_before_prune,
):
    table = fake_process_table
    ctx, removed = recovery_context(tmp_path)
    ctx.tracker.add_issue(1, "Runner crashed after launch", labels=["running"])
    with tick_processes(ctx.store) as snapshot:
        assert 42000 not in snapshot.rows
        root = table.add(42000, cwd=tmp_path, start_time=10)
        child = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=11)
        ctx.store.start_run(1, branch="branch", worktree=tmp_path / "worktree", pid=root.pid)
        table.exit(child.pid)
        table.exit(root.pid)
        if reuse_before_prune:
            table.add(root.pid, cwd=tmp_path, start_time=20)
    birth = ctx.store.get_run(1).process_identities
    if not reuse_before_prune:
        table.add(root.pid, cwd=tmp_path, start_time=20)
    report = scheduler.TickReport()
    with tick_processes(ctx.store):
        scheduler._recover_runs(ctx, report)
    run = ctx.store.get_run(1)
    assert run.status == "finished" and run.outcome == "crashed"
    assert birth == [{"pid": root.pid, "start_time": root.start_time}]
    assert not run.process_identities and not ctx.store.read_history()[0].process_identities
    assert "#1 runner crashed" in report.actions
    assert removed == [tmp_path / "worktree"]
    assert table.process(root.pid).start_time == 20
    assert not table.signals


@pytest.mark.parametrize("snapshot_state", ["unreadable_command", "unavailable"])
@pytest.mark.parametrize("discovery", ["alive", "unknown"])
def test_pidless_recovery_requires_fresh_runner_absence(
    fake_process_table, tmp_path, monkeypatch, snapshot_state, discovery,
):
    table = fake_process_table
    ctx, removed = recovery_context(tmp_path)
    ctx.tracker.add_issue(1, "Runner has no recorded PID", labels=["running"])
    run = ctx.store.start_run(1, branch="branch", worktree=tmp_path / "worktree")
    ctx.now = lambda: scheduler.datetime.fromisoformat(run.started_at) + scheduler.LAUNCH_GRACE
    root = table.add(42000, cwd=tmp_path, start_time=10)

    def listing():
        if snapshot_state == "unavailable":
            raise PermissionError("fake table unavailable")
        return (processes.ProcessIdentity(root.pid, root.ppid, root.pgid, root.sid, root.start_time, None),)

    probes = []

    def ps(*args, **kwargs):
        probes.append(args)
        if discovery == "unknown":
            raise PermissionError("fake runner discovery denied")
        return subprocess.CompletedProcess(args, 0, stdout="42000 fa gate run-issue 1\n")

    monkeypatch.setattr(processes, "list_processes", listing)
    monkeypatch.setattr(scheduler.subprocess, "run", ps)
    with processes.process_snapshot():
        scheduler._recover_runs(ctx, scheduler.TickReport())
    assert probes, "Snapshot absence cannot establish runner death"
    assert ctx.store.get_run(1).status == "running"
    assert root.pid in table.live
    assert not removed and not table.signals and not ctx.store.read_history()


def test_stop_expands_from_a_root_omitted_by_the_tick_snapshot(
    fake_process_table, tmp_path, monkeypatch,
):
    table = fake_process_table
    ctx, removed = recovery_context(tmp_path)
    root = table.add(42000, cwd=tmp_path, start_time=10)
    ctx.store.start_run(1, branch="branch", worktree=tmp_path / "worktree", pid=root.pid)
    child = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=11)
    cached = iter((tuple(row for row in table.list_processes() if row.pid != root.pid),))
    monkeypatch.setattr(processes, "list_processes", lambda: next(cached, None) or table.list_processes())
    with processes.process_snapshot() as snapshot:
        assert root.pid not in snapshot.rows and child.pid in snapshot.rows
        assert scheduler._kill_run(ctx, 1, scheduler.TickReport())
    assert not {root.pid, child.pid} & table.live
    assert [pid for _, pid, _ in table.signals] == [child.pid, root.pid]
    assert removed == [tmp_path / "worktree"]


def test_each_stop_discovers_children_forked_after_the_previous_stop(
    fake_process_table, tmp_path,
):
    table = fake_process_table
    roots = [table.add(pid, cwd=tmp_path, start_time=10) for pid in (42000, 43000)]
    with processes.process_snapshot():
        with processes.recorded_tree([{"pid": roots[0].pid, "start_time": roots[0].start_time}]):
            assert processes.kill_process_group(roots[0].pid) == 1
        child = table.add(43001, ppid=roots[1].pid, cwd=tmp_path, start_time=11)
        with processes.recorded_tree([{"pid": roots[1].pid, "start_time": roots[1].start_time}]):
            assert processes.kill_process_group(roots[1].pid) == 2
    assert not {root.pid for root in roots} & table.live
    assert child.pid not in table.live
    assert [pid for _, pid, _ in table.signals] == [roots[0].pid, child.pid, roots[1].pid]


def test_legacy_group_discovery_is_fresh_after_its_leader_exits(
    fake_process_table, tmp_path,
):
    table = fake_process_table
    ctx, removed = recovery_context(tmp_path)
    ctx.tracker.add_issue(1, "Legacy group", labels=["running"])
    root = table.add(42000, cwd=tmp_path, start_time=10)
    run = ctx.store.start_run(1, branch="branch", worktree=tmp_path / "worktree")
    ctx.store._write_run(replace(run, pid=root.pid, executor_pgid=root.pid))
    with processes.process_snapshot():
        child = table.add(42001, ppid=root.pid, pgid=root.pgid, sid=root.sid,
                          cwd=tmp_path, start_time=11)
        table.exit(root.pid)
        run = ctx.store.get_run(1)
        assert _run_processes_exist(run)
        assert _stop_run_processes(ctx, run, scheduler.TickReport()) == "refused"
        assert not scheduler._kill_run(ctx, 1, scheduler.TickReport())
    assert child.pid in table.live
    assert ctx.store.get_run(1).status == "running"
    assert not removed and not table.signals and not ctx.store.read_history()


def test_unavailable_legacy_group_discovery_refuses_without_escaping(
    fake_process_table, tmp_path, monkeypatch,
):
    table = fake_process_table
    ctx, removed = recovery_context(tmp_path)
    ctx.tracker.add_issue(1, "Unreadable legacy group", labels=["running"])
    run = ctx.store.start_run(1, branch="branch", worktree=tmp_path / "worktree")
    run = replace(run, pid=42000, executor_pgid=42000)
    ctx.store._write_run(run)

    def denied():
        raise PermissionError("fake process table denied")

    monkeypatch.setattr(processes, "list_processes", denied)
    with processes.process_snapshot() as snapshot:
        assert not snapshot.available
        assert _stop_run_processes(ctx, run, scheduler.TickReport()) == "refused"
        assert not scheduler._kill_run(ctx, 1, scheduler.TickReport())
    assert ctx.store.get_run(1).status == "running"
    assert not removed and not table.signals and not ctx.store.read_history()


@pytest.mark.parametrize("readable", [False, True])
def test_unavailable_tick_snapshot_keeps_explicit_stops_on_the_fresh_path(
    fake_process_table, tmp_path, monkeypatch, caplog, readable,
):
    table = fake_process_table
    ctx, removed = recovery_context(tmp_path)
    ctx.tracker.add_issue(1, "Explicit stop", labels=["running"])
    root = table.add(42000, cwd=tmp_path, start_time=10)
    child = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=11)
    run = ctx.store.start_run(1, branch="branch", worktree=tmp_path / "worktree", pid=root.pid)

    def unavailable():
        raise PermissionError("fake process table unavailable")

    monkeypatch.setattr(processes, "list_processes", unavailable)
    monkeypatch.setattr(ctx.store, "prune_process_identities", lambda **kwargs: pytest.fail("Unknown ownership cannot be pruned"))
    with tick_processes(ctx.store) as snapshot:
        assert not snapshot.available
        if readable:
            monkeypatch.setattr(processes, "list_processes", table.list_processes)
        result = _stop_run_processes(ctx, run, scheduler.TickReport())
    assert result == ("stopped" if readable else "refused")
    current = ctx.store.get_run(1)
    assert current.status == "running" and current.process_identities == run.process_identities
    if not readable:
        assert current.detail.startswith("Stop refused:")
        assert ctx.store.load_flag(1).reason == scheduler.Reason.STOP_REFUSED
    assert ({root.pid, child.pid} <= table.live) is not readable
    assert bool(table.signals) is readable
    assert len([record for record in caplog.records if record.name == "foragent.processes"]) == 1
    assert not removed and not ctx.store.read_history()


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_unavailable_table_preserves_orphan_worktree_and_running_label(
    fake_process_table, tmp_path, monkeypatch, caplog, error,
):
    table = fake_process_table
    ctx, removed = recovery_context(tmp_path)
    ctx.tracker.add_issue(1, "Orphan running label", labels=["running"])
    root = table.add(42000, cwd=tmp_path, start_time=10)
    ctx.store.start_run(1, branch="branch", worktree=tmp_path / "worktree", pid=root.pid)
    run = ctx.store.finish_run(1, "killed")
    ctx.git.fetch = lambda *args: None
    calls = []

    def unavailable():
        calls.append(True)
        raise error("fake process table unavailable")

    monkeypatch.setattr(processes, "list_processes", unavailable)
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0))
    report = scheduler.tick(ctx)
    assert report.skipped is None and not report.actions
    assert ctx.store.get_run(1) == run
    assert ctx.store.read_history() == [run]
    assert "running" in ctx.tracker.get_issue(1).labels
    assert root.pid in table.live
    assert len(calls) == 1
    assert len([record for record in caplog.records if record.name == "foragent.processes"]) == 1
    assert not removed and not table.signals
