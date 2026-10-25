"""Acceptance test for process-snapshot-per-tick. Locked by Foragent; do not edit."""

import json
import math
import time
from datetime import timedelta

import pytest
from test_orch_review_handoff import bind_cli
from test_skills_autosync import cli_exit
from test_stop_whole_tree import (
    FakeProcessTable,
    _inactive_and_running,
    record_tree,
    stop_case,
    stop_context,
)
from test_stop_whole_tree import (
    fake_processes as fake_processes,  # noqa: PLC0414 -- pytest autouse fixture re-export
)

from foragent import cli, processes, scheduler


@pytest.fixture(autouse=True)
def snapshot_io(fake_processes, monkeypatch):
    """A targeted pre-signal read is not a whole-table enumeration.

    conftest's generic identity reader delegates to list_processes. Model the
    real single-PID seam directly here, so list_calls measures only scans.
    All ownership, pruning, traversal and signaling decisions stay in production.
    """
    assert isinstance(fake_processes, FakeProcessTable)

    def read_identity(pid):
        try:
            return fake_processes.process(pid)
        except ProcessLookupError:
            return None

    monkeypatch.setattr(processes, "read_process_identity", read_identity)


def identities(case, number=1):
    return {(item["pid"], item["start_time"])
            for item in case.store.get_run(number).process_identities}


def checked_tick(case, table):
    # Exclude run-creation scans; include every production scan inside tick.
    table.list_calls = 0
    return scheduler.tick(stop_context(case, table))


def assert_one_listing(table, stops=0):
    # Routine liveness shares one snapshot; each tree being stopped may take one
    # fresh listing of its own, because a snapshot can be minutes old and miss a
    # child forked after it (that child would otherwise be orphaned to pid 1).
    assert table.list_calls <= 1 + stops, (
        f"One tick must share one process-table snapshot plus at most one fresh listing per stopped tree "
        f"({stops} stopped); got {table.list_calls} listings"
    )


def assert_only_signaled(table, owned):
    assert all(kind == "pid" and pid in owned for kind, pid, _ in table.signals), (
        f"Only verified recorded tree members may be signaled: {table.signals}"
    )
    assert not table.leaderless_group_calls, "A snapshot must not authorize a leaderless group"
    assert table.host_pids <= table.live, "Tick touched its own lineage or another host process"


@pytest.mark.parametrize("state", ["running", "finished", "mixed"])
def test_tick_lists_once_for_five_runs_with_three_hundred_identities(
    git_history, tmp_path, monkeypatch, fake_processes, state,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    table = fake_processes
    case.tracker.remove_labels(1, ["approved"])
    active, orphaned = set(), set()
    for number in range(1, 6):
        if number != 1:
            case.tracker.add_issue(number, f"Run {number}", labels=["running"])
        worktree = case.store.worktree_path(number)
        root_pid = 10000 + number * 1000
        rows = [table.add(
            root_pid + offset, ppid=1 if offset == 0 else root_pid,
            cwd=worktree, start_time=case.clock[0].timestamp() + offset,
        ) for offset in range(300)]
        case.store.start_run(number, branch=f"foragent/issue-{number}",
                             worktree=worktree, pid=root_pid)
        assert len(identities(case, number)) == 300, "Fixture must really record 300 identities"
        inactive = state == "finished" or (state == "mixed" and number % 2 == 0)
        if inactive:
            case.store.finish_run(number, "killed")
            case.tracker.remove_labels(number, ["running"])
            case.tracker.close_issue(number)
            # Exercise hundreds of stale candidates plus live, recorded orphans.
            for row in rows[3:]:
                table.exit(row.pid)
            orphaned.update(row.pid for row in rows[:3])
        else:
            active.update(row.pid for row in rows)

    report = checked_tick(case, table)

    assert report.skipped is None, report
    assert active <= table.live, "Snapshot confused an active tree with an orphan"
    assert not orphaned & table.live, "Verified inactive tree members survived cleanup"
    assert_only_signaled(table, orphaned)
    assert_one_listing(table)


def test_tick_prunes_exited_identities_and_bounds_growth_across_ticks(
    git_history, tmp_path, monkeypatch, fake_processes,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    table = fake_processes
    root, executor, helper, detached = record_tree(case, table)
    case.tracker.remove_labels(1, ["approved"])
    table.exit(helper.pid)
    table.exit(detached.pid)
    anchors = {(root.pid, root.start_time), (executor.pid, executor.start_time)}
    previous = None
    for cycle in range(4):
        if previous is not None:
            table.exit(previous.pid)
        child = table.add(45000 + cycle, ppid=executor.pid, cwd=tmp_path,
                          start_time=executor.start_time + 10 + cycle)
        checked_tick(case, table)
        recorded = identities(case)
        assert recorded == anchors | {(child.pid, child.start_time)}, (
            "Tick must prune exited identities and discover only the live descendant; "
            f"cycle {cycle}: retained {len(recorded)} identities instead of 3"
        )
        assert len(case.store.get_run(1).process_identities) == 3, "The stored list must not accumulate duplicates"
        assert case.store.get_run(1).status == "running"
        assert not table.signals, "A healthy active tree must not be signaled"
        assert_one_listing(table)
        previous = child
    table.exit(previous.pid)
    checked_tick(case, table)
    assert identities(case) == anchors, "The last exited child must also be pruned"
    assert len(case.store.get_run(1).process_identities) == 2
    assert_one_listing(table)


@pytest.mark.parametrize("state", ["running", "finished"])
@pytest.mark.parametrize("error_type", [PermissionError, OSError], ids=["denied", "unreadable"])
def test_tick_preserves_unknown_identities_until_a_readable_snapshot(
    git_history, tmp_path, monkeypatch, fake_processes, state, error_type,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    table = fake_processes
    root, executor, helper, detached = record_tree(case, table)
    case.tracker.remove_labels(1, ["approved"])
    if state == "finished":
        case.store.finish_run(1, "killed")
        case.tracker.remove_labels(1, ["running"])
        case.tracker.close_issue(1)
    before = identities(case)
    table.exit(helper.pid)
    table.exit(detached.pid)

    def unreadable():
        table.list_calls += 1
        raise error_type("Fake process table cannot be read")

    with monkeypatch.context() as denied:
        denied.setattr(processes, "list_processes", unreadable)
        # The contract does not require hiding I/O errors, only preserving
        # unknown liveness and avoiding repeated enumeration attempts.
        try:
            checked_tick(case, table)
        except OSError:
            pass
        assert identities(case) == before, "Unreadable liveness must never be treated as death"
        assert not table.signals, "Unknown ownership must not authorize a signal"
        assert case.store.get_run(1).status == state
        assert_one_listing(table)

    checked_tick(case, table)
    live_at_snapshot = {(root.pid, root.start_time), (executor.pid, executor.start_time)}
    assert identities(case) <= live_at_snapshot, "A readable tick must finally prune identities proven dead"
    if state == "running":
        assert identities(case) == live_at_snapshot, "Verified live identities must be retained"
    else:
        assert not {root.pid, executor.pid} & table.live, "A readable tick must resume verified orphan cleanup"
        assert_only_signaled(table, {root.pid, executor.pid})
    assert_one_listing(table)
    if state == "finished":
        # A process stopped after enumeration can remain in that snapshot.
        # The next tick must see its exit and persist an empty ownership list.
        checked_tick(case, table)
        assert identities(case) == set(), "The next snapshot must prune the reaped identities"
        assert_one_listing(table)


@pytest.mark.parametrize("evidence", ["cwd", "argv"])
def test_tick_snapshot_reaps_only_recorded_inactive_trees(
    git_history, tmp_path, monkeypatch, fake_processes, evidence,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    table = fake_processes
    orphans, protected = _inactive_and_running(case, table, evidence=evidence)

    checked_tick(case, table)

    assert protected <= table.live, "Tick touched an active tree, an unowned lookalike or a never-launched issue"
    assert case.store.get_run(2).status == "running"
    assert case.store.get_run(3) is None, "Never-launched worktree must have no ownership record"
    assert not set(orphans) & table.live, "Recorded orphan descendants must be reaped"
    assert_only_signaled(table, set(orphans))
    assert_one_listing(table, stops=1)
    signals = list(table.signals)
    checked_tick(case, table)
    assert identities(case) == set(), "The next snapshot must remove reaped orphan identities"
    assert protected <= table.live, "A later tick must preserve the same protected processes"
    assert table.signals == signals, "A later tick must not signal already reaped identities again"
    assert_one_listing(table)


@pytest.mark.parametrize("state", ["running", "finished"])
@pytest.mark.parametrize("identity", ["runner", "executor"])
def test_tick_snapshot_preserves_reused_pid_and_its_unrecorded_child(
    git_history, tmp_path, monkeypatch, fake_processes, state, identity,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    table = fake_processes
    tree = record_tree(case, table)
    case.tracker.remove_labels(1, ["approved"])
    if state == "finished":
        case.store.finish_run(1, "killed")
        case.tracker.remove_labels(1, ["running"])
        case.tracker.close_issue(1)
    original = tree[0 if identity == "runner" else 1]
    table.exit(original.pid)
    replacement = table.add(original.pid, cwd=case.store.worktree_path(1),
                            start_time=original.start_time + 100, argv=("unrelated-app",))
    child = table.add(44000, ppid=replacement.pid, pgid=replacement.pid, sid=replacement.pid,
                      cwd=case.store.worktree_path(1), start_time=replacement.start_time + 1)
    case.clock[0] += timedelta(minutes=case.config.max_runtime_minutes + 11)

    checked_tick(case, table)

    owned = {row.pid for row in tree} - {original.pid}
    assert {replacement.pid, child.pid} <= table.live, "Changed start time must reject a reused PID and its child"
    assert not owned & table.live, "PID reuse must not prevent cleanup of other recorded descendants"
    assert_only_signaled(table, owned)
    assert_one_listing(table, stops=1)


def test_tick_snapshot_rechecks_identity_if_pid_is_reused_during_stop(
    git_history, tmp_path, monkeypatch, fake_processes,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    table = fake_processes
    tree = record_tree(case, table)
    case.store.finish_run(1, "killed")
    case.tracker.remove_labels(1, ["approved", "running"])
    case.tracker.close_issue(1)
    replaced = []

    def reuse_after_first_signal(call):
        table.after_signal = None
        # Whichever traversal order production chooses, replace an as-yet
        # unsignaled member after the snapshot, before its eventual signal.
        victim = next(row for row in tree if row.pid in table.live and row.pid != call[1])
        table.exit(victim.pid)
        table.add(victim.pid, cwd=tmp_path, start_time=victim.start_time + 100)
        replaced.append(victim.pid)

    table.after_signal = reuse_after_first_signal
    checked_tick(case, table)

    assert len(replaced) == 1, "Fixture must reuse a PID during a real production stop"
    assert replaced[0] in table.live, "A tick snapshot cannot authorize signaling a later PID lifetime"
    owned = {row.pid for row in tree} - set(replaced)
    assert not owned & table.live, "Other verified members of the inactive tree survived"
    assert_only_signaled(table, owned)
    assert_one_listing(table, stops=1)


def test_tick_snapshot_never_signals_a_run_without_recorded_identities(
    git_history, tmp_path, monkeypatch, fake_processes,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    table = fake_processes
    tree = record_tree(case, table)
    case.store.finish_run(1, "killed")
    case.tracker.remove_labels(1, ["approved", "running"])
    case.tracker.close_issue(1)
    case.tracker.add_issue(2, "Legacy run without identities", labels=["running"])
    worktree = case.store.worktree_path(2)
    stranger = table.add(43000, cwd=worktree, start_time=10)
    child = table.add(43001, ppid=stranger.pid, cwd=worktree, start_time=11)
    case.store.start_run(2, branch="foragent/issue-2", worktree=worktree)
    case.store.update_run(2, pid=stranger.pid, executor_pgid=stranger.pid)
    assert identities(case, 2) == set(), "Numeric PID fields must not attest ownership"
    case.clock[0] += timedelta(minutes=case.config.max_runtime_minutes + 11)

    checked_tick(case, table)

    assert {stranger.pid, child.pid} <= table.live, "A run without recorded identities must receive no signal"
    assert case.store.get_run(2).status == "running", "An unverified live run must not be declared dead"
    assert identities(case, 2) == set(), "Tick must not adopt an unverified numeric PID"
    owned = {row.pid for row in tree}
    assert not owned & table.live, "The independently recorded inactive run must still be cleaned up"
    assert_only_signaled(table, owned)
    assert_one_listing(table)


@pytest.mark.parametrize("failed", [False, True], ids=["success", "failure"])
def test_gate_tick_records_duration_seconds_and_exposes_it_in_status(
    git_history, tmp_path, monkeypatch, fake_processes, capsys, failed,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    case.store.finish_run(1, "cancelled")
    case.tracker.remove_labels(1, ["approved", "running"])
    case.tracker.close_issue(1)
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "TickContext", lambda **kwargs: stop_context(case, fake_processes))

    def unexpected(*args, **kwargs):
        pytest.fail("Duration acceptance must not launch workers or notifications")

    monkeypatch.setattr(cli, "spawn_runner", lambda *args: unexpected)
    monkeypatch.setattr(cli, "spawn_notify", lambda *args: None)
    elapsed = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(time, "perf_counter", lambda: elapsed[0])
    fetch = case.git.fetch

    for seconds in (2.75, 1.25):
        def timed_fetch(*args, duration=seconds, **kwargs):
            result = fetch(*args, **kwargs)
            elapsed[0] += duration
            case.clock[0] += timedelta(seconds=duration)
            if failed:
                raise RuntimeError("Fake tick failure after measured work")
            return result

        monkeypatch.setattr(case.git, "fetch", timed_fetch)
        capsys.readouterr()
        try:
            code = cli_exit(main, ["gate", "tick", "--no-start", "--json", "--repo-root", str(case.root)])
        except RuntimeError as exc:
            assert failed and str(exc) == "Fake tick failure after measured work"
            code = 1
        output = capsys.readouterr()
        if failed:
            assert code not in (None, 0), "A failed tick must exit nonzero"
        else:
            assert code == 0, output.err
        path = case.store.root / "tick-outcome.json"
        assert path.is_file(), "Every completed or failed tick needs a durable outcome"
        outcome = json.loads(path.read_text())
        assert outcome["status"] == ("failed" if failed else "ok")

        assert cli_exit(main, ["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["tick_outcome"] == outcome, "gate status must expose this tick's persisted outcome"
        assert "duration_seconds" in outcome, "tick-outcome.json must record duration_seconds"
        duration = outcome["duration_seconds"]
        assert type(duration) in (int, float) and math.isfinite(duration), "Duration must be a finite number of seconds"
        assert duration == pytest.approx(seconds), "Record elapsed seconds for this tick, preserving fractions"
        assert status["tick_outcome"]["duration_seconds"] == duration
        assert not fake_processes.signals, "Measuring an idle tick must not signal anything"

def test_tick_stop_signals_a_child_forked_after_the_snapshot(
    git_history, tmp_path, monkeypatch, fake_processes,
):
    case = stop_case(git_history, tmp_path, monkeypatch)
    table = fake_processes
    tree = record_tree(case, table)
    case.store.finish_run(1, "killed")
    case.tracker.remove_labels(1, ["approved", "running"])
    case.tracker.close_issue(1)
    listing = table.list_processes
    late = []

    def list_then_fork():
        rows = listing()
        if not late:
            # A recorded member forks a child right after the tick's snapshot.
            parent = tree[2]
            late.append(table.add(45000, ppid=parent.pid, pgid=parent.pgid, sid=parent.sid,
                                  cwd=case.store.worktree_path(1), start_time=parent.start_time + 50))
        return rows

    monkeypatch.setattr(processes, "list_processes", list_then_fork, raising=False)
    checked_tick(case, table)

    assert late, "Fixture must fork a child after the first listing"
    assert late[0].pid not in table.live, "A child forked after the tick snapshot must be stopped with its tree"
    owned = {row.pid for row in tree} | {late[0].pid}
    assert not owned & table.live, "Every member of the stopped tree must end"
    assert_only_signaled(table, owned)
    assert_one_listing(table, stops=1)
