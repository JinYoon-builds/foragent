"""Boundary tests for lane dreams (#189): shared run recovery, admission, inputs and publication.

Process tests use fake process tables only: no real process is listed or signaled.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import orch_dream, orch_dream_worker, processes, scheduler
from foragent.config import Config
from foragent.state import StateStore

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
KEY = "dream-orch"


def row(pid, start=1.0, argv=None, uid=None):
    return SimpleNamespace(pid=pid, start_time=start, argv=argv, ppid=1, pgid=pid, sid=pid, uid=uid)


class NoIssues:
    """A dream never touches issue labels, comments or PRs during recovery."""

    def __getattr__(self, name):
        pytest.fail(f"dream recovery called the tracker: {name}")


@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    config = Config(repo="o/r", lanes={"orch": {"paths": ["orch/**"]}}, stall_minutes=10, max_runtime_minutes=60)
    killed, removed = [], []
    table = [processes.ProcessTable([])]

    def kill(pid):
        """Like kill_process_group: only recorded identities that still match are stopped."""
        recorded = processes._recorded_tree.get()
        assert recorded is not None, "a stop needs recorded ownership"
        killed.append(pid)
        owned = {(item["pid"], item["start_time"]) for item in recorded}
        current = table[0]  # Unreadable processes stay unreadable: the killer cannot see them.
        table[0] = processes.ProcessTable([item for item in current if (item.pid, item.start_time) not in owned],
                                          current.unreadable, current.unreadable_uids)

    ctx = SimpleNamespace(store=store, config=config, now=lambda: clock[0], kill_runner=kill, tracker=NoIssues(),
                          repo_root=(tmp_path / "repo").resolve(), git=SimpleNamespace(remove_worktree=removed.append),
                          runner_process_exists=lambda number: pytest.fail("issue runner discovery for a dream"))
    monkeypatch.setattr(processes, "list_processes", lambda: table[0])
    for name in ("observe_tree", "observe_routine_tree"):  # Recorded identities stay as recorded.
        monkeypatch.setattr(processes, name, lambda identities=(), roots=(), **kw: [dict(i) for i in identities])
    monkeypatch.setattr("foragent.sleep_credit.credit", lambda store, now: 0)
    alive = {}
    # Like os.kill(pid, 0): a listed pid exists, unless a test says otherwise.
    def exists(pid):
        return alive.get(pid, any(item.pid == pid for item in table[0]))

    for where in ("foragent.scheduler_stop._pid_exists", "foragent.scheduler_recovery._pid_exists"):
        monkeypatch.setattr(where, exists)
    return SimpleNamespace(store=store, config=config, ctx=ctx, clock=clock, killed=killed,
                           removed=removed, table=table, alive=alive)


def dreaming(world, *, pid=4100, start=1.0, executor=None, extra=()):
    world.store.start_dream("orch", worktree=world.store.root / "worktrees/dream-orch")
    identities = ([{"pid": pid, "start_time": start}] if start is not None and pid is not None else []) + list(extra)
    world.store.update_run(KEY, pid=pid, executor_pgid=executor, process_identities=identities)
    return world.store.get_dream("orch")


def prune(world):
    """Run the tick's identity prune with the fake table as the only process evidence."""
    from unittest import mock

    def evidence(pid, start_time, **kwargs):
        return processes.IdentityEvidence(any(item.pid == pid and item.start_time == start_time
                                              for item in world.table[0]))

    with mock.patch.object(processes, "current_snapshot", lambda: SimpleNamespace(available=True)), \
            mock.patch.object(processes, "recorded_identity_evidence", evidence):
        world.store.prune_process_identities()


def recover(world):
    report = SimpleNamespace(actions=[])
    scheduler._recover_runs(world.ctx, report)
    return report


def merged_run(world, number=3):
    world.store.start_run(number, branch="b", worktree="w", lane="orch")
    world.store.finish_run(number, outcome="merged")


def due(world):
    return orch_dream.dream_due(world.store, world.config, SimpleNamespace(list_open_issues=lambda: []),
                                world.clock[0])


def test_reused_worker_pid_finishes_the_dream_as_error_and_the_lane_is_due(world):
    merged_run(world)
    dreaming(world)  # Worker 4100 recorded with start time 1.0.
    world.table[0] = processes.ProcessTable([row(4100, start=9.0)])  # Same pid, another process.
    world.alive[4100] = True  # os.kill(4100, 0) succeeds for the reused pid.
    recover(world)
    finished = world.store.get_dream("orch")
    assert finished.status == "finished" and finished.outcome == "error"
    assert world.table[0][0].pid == 4100, "the unrelated process that reused the pid is not stopped"
    assert world.removed and due(world) == [], "its recorded identity waits for the tick's prune"
    prune(world)  # The tick's process pass: 4100@1.0 is confirmed gone (the pid now has another start).
    assert due(world) == ["orch"]
    history = (world.store.root / "runs.jsonl").read_text().splitlines()
    assert [json.loads(line).get("kind", "issue") for line in history] == ["issue"], "dreams stay out of runs.jsonl"


def test_stalled_dream_is_stopped_through_its_recorded_identity_and_ends_as_error(world):
    dreaming(world)
    world.table[0] = processes.ProcessTable([row(4100)])
    world.clock[0] += timedelta(minutes=11)
    recover(world)
    finished = world.store.get_dream("orch")
    assert world.killed == [4100] and world.table[0] == ()
    assert finished.outcome == "error" and "stall" in finished.detail


def test_dream_over_its_runtime_limit_is_stopped(world):
    dreaming(world)
    world.table[0] = processes.ProcessTable([row(4100)])
    world.clock[0] += timedelta(minutes=world.config.max_runtime_minutes + 11)
    world.store.update_run(KEY, last_progress_at=world.clock[0].isoformat())  # Progressing, but too long.
    recover(world)
    finished = world.store.get_dream("orch")
    assert world.killed == [4100] and finished.outcome == "error" and "runtime" in finished.detail


def test_recovery_decides_from_a_fresh_record_read(world, monkeypatch):
    dreaming(world)
    world.clock[0] += timedelta(minutes=11)  # The snapshot from active_runs() looks stalled.

    def listing():  # The worker records progress while the tick checks its process.
        world.store.update_run(KEY, last_progress_at=world.clock[0].isoformat())
        return processes.ProcessTable([row(4100)])

    monkeypatch.setattr(processes, "list_processes", listing)
    world.alive[4100] = True
    recover(world)
    assert world.killed == [] and world.store.get_dream("orch").status == "running"


@pytest.mark.parametrize("exists", [True, False])
def test_null_start_time_means_alive_while_the_pid_exists(world, exists):
    dreaming(world, start=None)
    world.alive[4100] = exists
    recover(world)
    run = world.store.get_dream("orch")
    assert (run.status, run.outcome) == (("running", None) if exists else ("finished", "error"))


def test_dead_worker_stops_its_surviving_executor_before_finishing(world):
    dreaming(world, executor=4200, extra=[{"pid": 4200, "start_time": 2.0}])
    world.table[0] = processes.ProcessTable([row(4200, start=2.0)])  # The worker is gone; its executor lives.
    recover(world)
    assert world.table[0] == () and world.killed
    assert world.store.get_dream("orch").outcome == "error"


def test_failed_launch_without_a_pid_ends_as_error_after_the_grace(world):
    from foragent import scheduler

    dreaming(world, pid=None, start=None)
    recover(world)
    assert world.store.get_dream("orch").status == "running", "within the launch grace"
    world.clock[0] += scheduler.LAUNCH_GRACE + timedelta(seconds=1)
    recover(world)
    assert world.store.get_dream("orch").outcome == "error" and world.killed == []


def test_manual_completion_refuses_only_a_confirmed_live_worker(world):
    run = dreaming(world)
    world.table[0] = processes.ProcessTable([row(4100)])  # Same pid and start time: alive.
    assert orch_dream.dreamed_command(world.store, world.config, "orch") == 2
    assert orch_dream.last_dream_at(world.store, "orch") is None
    world.table[0] = processes.ProcessTable([row(4100, start=9.0)])  # Reused pid: not alive.
    world.alive[4100] = True
    assert orch_dream.dreamed_command(world.store, world.config, "orch") == 0
    assert orch_dream.last_dream_at(world.store, "orch") is not None
    assert world.store.get_dream("orch") == run, "the command never writes the dream record"


def worker_world(world, executor_edit, publish):
    class Executor:
        def run(self, **kwargs):
            executor_edit(kwargs["worktree"])
            from foragent.executors import ExecResult
            return ExecResult(0, False, "done", kwargs["log_path"])

    def add_worktree(path, branch, ref):
        path.mkdir(parents=True)

    def remove_worktree(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    calls = {"commits": [], "published": []}
    head = "pushed-sha"
    git = SimpleNamespace(
        _call=lambda args: head if args[:1] == ["rev-parse"] else ("MEMORY.md\n" if args[:1] == ["diff"] else ""),
        commit_paths=lambda paths, title: calls["commits"].append(paths))
    world.ctx.git = SimpleNamespace(_call=lambda args: head if args[:1] == ["rev-parse"] else "",
                                    add_worktree=add_worktree, remove_worktree=remove_worktree, at=lambda path: git)
    world.ctx.executor_factory = lambda name: Executor()
    world.ctx.poll_hook = lambda execute, poll: execute()
    world.ctx.tracker = None

    def fake_publish(ctx, git, lane, memory, count, **kwargs):
        calls["published"].append(count)
        return publish()

    return calls, fake_publish


def test_start_rechecks_a_lane_whose_issue_started_this_tick(world, monkeypatch):
    from foragent import scheduler

    monkeypatch.setattr(scheduler, "_has_slot", lambda ctx, budget: True)
    monkeypatch.setattr(orch_dream, "dream_due", lambda *args: [])  # An issue of orch started meanwhile.
    world.ctx.tracker = SimpleNamespace(find_pr=lambda branch: None)
    started = []
    world.ctx.launcher = lambda **kwargs: started.append(kwargs)
    orch_dream.start(world.ctx, ["orch"], 2, SimpleNamespace(actions=[]))
    assert started == [] and world.store.get_dream("orch") is None


def test_prompt_lists_every_run_since_the_last_dream(world):
    for number in range(1, 61):
        world.clock[0] += timedelta(seconds=1)
        world.store.start_run(number, branch="b", worktree="w", lane="orch")
        world.store.finish_run(number, outcome="merged")
    text = orch_dream_worker.prompt(world.store, world.config, "orch", {})
    assert all(f"- #{number} merged" in text for number in range(1, 61))


def test_completion_covers_runs_until_the_prompt_not_until_the_event(world):
    covered = world.clock[0]
    world.clock[0] += timedelta(minutes=5)
    world.store.start_run(7, branch="b", worktree="w", lane="orch")
    world.clock[0] -= timedelta(minutes=2)  # Finished while the dream ran, after its prompt.
    world.store.finish_run(7, outcome="merged")
    world.clock[0] += timedelta(minutes=10)
    orch_dream.record_done(world.store, "orch", 0, "en", covered_until=covered)
    assert orch_dream.last_dream_at(world.store, "orch") == covered
    assert [run.number for run, _ in orch_dream._runs_since(world.store, "orch", covered)] == [7]


def test_media_merges_another_dreams_later_write_and_keeps_a_conflict(tmp_path):
    target, proposal = tmp_path / "media/orch.md", tmp_path / "proposal.md"
    target.parent.mkdir()
    snapshot = "one\ntwo\nthree\n"
    target.write_text("zero\none\ntwo\nthree\n")  # Another repository's dream added a line on top.
    proposal.write_text("one\ntwo\nthree\nfour\n")
    assert orch_dream_worker.publish_media(proposal, target, snapshot).startswith("wrote")
    assert target.read_text() == "zero\none\ntwo\nthree\nfour\n"
    target.write_text("one\nTWO\nthree\nfour\n")
    proposal.write_text("one\n2\nthree\nfour\n")
    assert orch_dream_worker.publish_media(proposal, target, "zero\none\ntwo\nthree\nfour\n").startswith("kept")
    assert target.read_text() == "one\nTWO\nthree\nfour\n"


def test_dream_done_event_names_the_pr_and_reference(world):
    event = orch_dream.record_done(world.store, "orch", 3, "ko", pr=12)
    saved = json.loads((world.store.root / "orch-events.jsonl").read_text().splitlines()[-1])
    assert saved["pr"] == 12 and "references/dreams.md" in event["summary"]
    assert event["say"] == "orch 레인이 꿈을 꿨어요 — 기억 3개 정리"


def test_start_rechecks_quota_exhausted_after_the_candidates(world, monkeypatch):
    from foragent import scheduler

    monkeypatch.setattr(scheduler, "_has_slot", lambda ctx, budget: True)
    monkeypatch.setattr(orch_dream, "dream_due", lambda *args: ["orch"])
    world.ctx.tracker = SimpleNamespace(find_pr=lambda branch: None)
    world.store.record_usage("claude", [{"name": "5h", "used_percent": 100,
                                         "resets_at": (NOW + timedelta(hours=1)).isoformat()}])
    started = []
    world.ctx.launcher = lambda **kwargs: started.append(kwargs)
    orch_dream.start(world.ctx, ["orch"], 2, SimpleNamespace(actions=[]))
    assert started == [] and world.store.get_dream("orch") is None


def test_new_memory_file_in_an_ignored_directory_is_published_and_counted(world, tmp_path):
    worktree = world.store.root / "worktrees/dream-orch"
    committed, published = [], []

    class Executor:
        def run(self, **kwargs):
            path = kwargs["worktree"] / ".claude/agent-memory/orch/MEMORY.md"
            path.parent.mkdir(parents=True)
            path.write_text("one lesson\ntwo lesson\n")
            from foragent.executors import ExecResult
            return ExecResult(0, False, "done", kwargs["log_path"])

    def add_worktree(path, branch, ref):
        path.mkdir(parents=True)

    def remove_worktree(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    # git status shows nothing: the memory directory is ignored.
    git = SimpleNamespace(_call=lambda args: "", commit_paths=lambda paths, title: committed.append(paths))
    world.ctx.git = SimpleNamespace(_call=lambda args: "", add_worktree=add_worktree,
                                    remove_worktree=remove_worktree, at=lambda path: git)
    world.ctx.executor_factory = lambda name: Executor()
    world.ctx.poll_hook = lambda execute, poll: execute()
    world.ctx.tracker = None
    original = orch_dream_worker._publish
    orch_dream_worker._publish = lambda ctx, git, lane, memory, count, **kw: published.append(count) or 7
    try:
        dreaming(world)
        assert orch_dream_worker.run_dream("orch", world.ctx) == 0
    finally:
        orch_dream_worker._publish = original
    assert committed == [[".claude/agent-memory/orch"]] and published == [2]
    saved = json.loads((world.store.root / "orch-events.jsonl").read_text().splitlines()[-1])
    assert saved["memories"] == 2 and saved["pr"] == 7
    assert not worktree.exists()


def committed_lanes(world):
    from foragent.scheduler_config import record_tick_config

    content = ("repo: o/r\nlanes:\n  orch:\n    paths: ['orch/**']\n    agent: fa-new\n    executor: codex\n"
               "  docs:\n    paths: ['docs/**']\n")
    record_tick_config(world.store, "origin/main:.foragent.yml", content)
    return Config(repo="o/r", executor="claude", lanes={"orch": {"paths": ["orch/**"], "agent": "fa-old"}})


def test_worker_runs_with_the_config_the_tick_admitted_it_with(world, monkeypatch):
    checkout = committed_lanes(world)
    seen = []
    monkeypatch.setattr(orch_dream_worker, "run_dream", lambda lane, ctx: seen.append(ctx.config) or 0)
    ctx = SimpleNamespace(store=world.store, config=checkout)
    assert orch_dream.run_dream("orch", ctx) == 0
    from foragent.run_implementation import lane_config

    assert orch_dream.memory_dir(seen[0], "orch") == "fa-new"
    assert lane_config(seen[0], "orch").executor == "codex"


def test_manual_completion_accepts_a_lane_configured_in_the_committed_config(world):
    checkout = committed_lanes(world)
    assert "docs" not in checkout.lanes
    assert orch_dream.dreamed_command(world.store, checkout, "docs") == 0
    assert orch_dream.last_dream_at(world.store, "docs") is not None


def test_active_dreams_are_no_issue_rows_or_issue_logs(world):
    from foragent import status

    for lane in ("orch", "view"):
        world.store.start_dream(lane, worktree=world.store.root / f"worktrees/dream-{lane}")
    world.store.start_run(5, branch="b", worktree="w", lane="orch")
    config = Config(repo="o/r", lanes={name: {"paths": [f"{name}/**"]} for name in ("orch", "view")})
    collected = status.collect(world.store, config, world.clock[0])
    assert [run["number"] for run in collected["active_runs"]] == [5]
    rows = [item for row in collected["lanes"].values() for state in row.values()
            if isinstance(state, list) for item in state]
    assert not any(isinstance(item, dict) and item.get("number") == 0 for item in rows)
    assert collected["summary"]["working"] == 1


def test_unreadable_worker_is_unknown_so_recovery_keeps_the_dream(world):
    dreaming(world)
    world.table[0] = processes.ProcessTable([], unreadable=[4100])  # Alive, but its row cannot be read.
    world.alive[4100] = True
    recover(world)
    assert world.store.get_dream("orch").status == "running" and world.removed == []


def test_manual_completion_with_unknown_liveness_only_appends_the_event(world):
    run = dreaming(world)
    world.table[0] = processes.ProcessTable([], unreadable=[4100])
    world.alive[4100] = True
    assert orch_dream.dreamed_command(world.store, world.config, "orch") == 0
    assert orch_dream.last_dream_at(world.store, "orch") is not None
    assert world.store.get_dream("orch") == run


def test_refused_stop_with_an_unreadable_recorded_executor_keeps_the_dream(world):
    dreaming(world, executor=4200, extra=[{"pid": 4200, "start_time": 2.0}])
    world.table[0] = processes.ProcessTable([], unreadable=[4200])  # Worker gone; executor unreadable.

    def refuse(pid):
        raise ValueError("ownership cannot be verified")

    world.ctx.kill_runner = refuse
    report = recover(world)
    assert world.store.get_dream("orch").status == "running" and world.removed == []
    assert any("Stop refused" in action for action in report.actions)


def test_stalled_dream_whose_stop_cannot_be_confirmed_keeps_its_record(world):
    dreaming(world)
    world.table[0] = processes.ProcessTable([], unreadable=[4100])  # The killer cannot see it.
    world.alive[4100] = True
    world.clock[0] += timedelta(minutes=11)
    report = recover(world)
    run = world.store.get_dream("orch")
    assert run.status == "running" and world.removed == []
    assert any("cannot be confirmed" in action for action in report.actions)


def test_progress_recorded_after_the_stall_check_voids_the_kill(world, monkeypatch):
    dreaming(world)
    world.table[0] = processes.ProcessTable([row(4100)])
    world.clock[0] += timedelta(minutes=11)

    def wake_grace(store, run, now):  # Evaluated inside the stall check, before the stop.
        world.store.update_run(KEY, last_progress_at=now.isoformat())
        return False

    monkeypatch.setattr("foragent.sleep_credit.in_wake_grace", wake_grace)
    recover(world)
    assert world.killed == [] and world.store.get_dream("orch").status == "running"


def started(world, launcher, monkeypatch):
    from foragent import scheduler

    monkeypatch.setattr(scheduler, "_has_slot", lambda ctx, budget: True)
    monkeypatch.setattr(orch_dream, "dream_due", lambda *args: ["orch"])
    world.ctx.tracker = SimpleNamespace(find_pr=lambda branch: None)
    world.ctx.launcher = launcher
    orch_dream.start(world.ctx, ["orch"], 2, SimpleNamespace(actions=[]))
    return world.store.get_dream("orch")


def test_launcher_without_a_process_is_a_failed_start_and_the_lane_stays_due(world, monkeypatch):
    merged_run(world)
    run = started(world, lambda **kwargs: None, monkeypatch)
    assert (run.status, run.outcome) == ("finished", "error")
    assert world.store.active_runs() == [] and orch_dream.last_dream_at(world.store, "orch") is None


def test_in_process_worker_that_finished_is_left_alone(world, monkeypatch):
    def worker(**kwargs):  # Runs synchronously and completes before the launcher returns.
        orch_dream.record_done(world.store, "orch", 0, "en")
        world.store.finish_run(KEY, "dreamed", "0 memory lines tidied.")

    run = started(world, worker, monkeypatch)
    assert (run.status, run.outcome) == ("finished", "dreamed")
    assert orch_dream.last_dream_at(world.store, "orch") is not None


MEMORY = ".claude/agent-memory/orch/MEMORY.md"


def test_leftover_processes_of_a_finished_dream_are_reaped(world, monkeypatch):
    dreaming(world, extra=[{"pid": 4300, "start_time": 3.0}])
    world.store.finish_run(KEY, "dreamed", "0 memory lines tidied.")
    world.table[0] = processes.ProcessTable([row(4300, start=3.0)])  # The executor's child survived.
    reaped = []

    def kill_group(pid):
        reaped.extend(item["pid"] for item in processes._recorded_tree.get())
        world.table[0] = processes.ProcessTable([])
        return 1

    monkeypatch.setattr(processes, "kill_process_group", kill_group)
    report = SimpleNamespace(actions=[])
    scheduler._reap_inactive_runs(world.ctx, report)
    assert 4300 in reaped and any("Reaped 1" in action for action in report.actions)


def test_a_lane_does_not_dream_while_an_earlier_dream_still_owns_processes(world):
    merged_run(world)
    dreaming(world, extra=[{"pid": 4300, "start_time": 3.0}])
    world.store.finish_run(KEY, "error", "Dream process died.")
    assert due(world) == [], "the leftovers stay owned by the earlier record until they are reaped"
    finished = world.store.get_dream("orch")
    world.store._write_run(replace(finished, process_identities=[]))  # Reaped or pruned as gone.
    assert due(world) == ["orch"]
    assert world.store.start_dream("orch", worktree="w").process_identities == []


def test_new_dream_reusing_a_pid_without_a_readable_birth_is_alive(world):
    dreaming(world)  # The earlier dream's worker 4100 was born at 1.0.
    world.store.finish_run(KEY, "error", "Dream process died.")
    world.store._write_run(replace(world.store.get_dream("orch"), process_identities=[]))
    world.store.start_dream("orch", worktree=world.store.root / "worktrees/dream-orch")
    world.store.update_run(KEY, pid=4100)  # A new worker got pid 4100; its birth could not be read.
    world.alive[4100] = True
    recover(world)
    run = world.store.get_dream("orch")
    assert run.status == "running" and world.removed == []


def test_finishing_a_dream_observes_its_tree_like_an_issue_run(world, monkeypatch):
    dreaming(world)
    child = {"pid": 4400, "start_time": 4.0}
    monkeypatch.setattr(processes, "observe_tree",
                        lambda identities=(), roots=(), **kw: [*map(dict, identities), child])
    finished = world.store.finish_run(KEY, "dreamed", "done")
    assert child in finished.process_identities


def test_stop_of_a_dream_worker_without_a_birth_identity_is_confirmed_by_its_pid(world):
    dreaming(world, start=None, executor=4200, extra=[{"pid": 4200, "start_time": 2.0}])
    world.table[0] = processes.ProcessTable([row(4200, start=2.0)])
    world.alive[4100] = True  # The worker lives; only the recorded executor tree can be stopped.
    world.clock[0] += timedelta(minutes=11)
    report = recover(world)
    assert world.table[0] == (), "the executor was stopped"
    assert world.store.get_dream("orch").status == "running" and world.removed == []
    assert any("cannot be confirmed" in action for action in report.actions)


def test_worker_that_registers_after_the_dead_check_is_not_finished(world, monkeypatch):
    from foragent import scheduler as facade

    dreaming(world, pid=None, start=None)
    world.clock[0] += facade.LAUNCH_GRACE + timedelta(seconds=1)  # Looks like a failed launch.
    original = world.store.get_run
    reads = []

    def late(key):  # The delayed worker registers between the dead check and the reread.
        reads.append(key)
        if key == KEY and len(reads) == 2:  # update_run's own read, then the reread after the dead check.
            monkeypatch.setattr(world.store, "get_run", original)
            world.store.update_run(KEY, pid=4100, process_identities=[{"pid": 4100, "start_time": 1.0}],
                                   last_progress_at=world.clock[0].isoformat())
        return original(key)

    monkeypatch.setattr(world.store, "get_run", late)
    world.table[0] = processes.ProcessTable([row(4100)])
    recover(world)
    run = original(KEY)
    assert run.status == "running" and run.pid == 4100 and world.removed == []


def test_surviving_executor_without_a_birth_identity_stays_recorded(world):
    dreaming(world, executor=4200)  # The worker has a birth identity; the executor has none.
    world.table[0] = processes.ProcessTable([row(4100)])
    world.alive[4200] = True
    world.clock[0] += timedelta(minutes=11)
    recover(world)
    run = world.store.get_dream("orch")
    assert run.status == "running" and run.executor_pgid == 4200, "the next tick must still see the executor"


def test_worker_registering_during_the_executor_stop_keeps_its_record_and_worktree(world):
    from foragent import scheduler as facade

    dreaming(world, pid=None, start=None, executor=4200, extra=[{"pid": 4200, "start_time": 2.0}])
    world.table[0] = processes.ProcessTable([row(4200, start=2.0)])
    world.clock[0] += facade.LAUNCH_GRACE + timedelta(seconds=1)  # Looks like a failed launch.
    stop = world.ctx.kill_runner

    def kill_and_register(pid):  # The delayed worker registers while its executor is stopped.
        stop(pid)
        world.store.update_run(KEY, pid=4100, process_identities=[{"pid": 4100, "start_time": 1.0}],
                               last_progress_at=world.clock[0].isoformat())

    world.ctx.kill_runner = kill_and_register
    recover(world)
    run = world.store.get_dream("orch")
    assert run.status == "running" and run.pid == 4100 and world.removed == []


def test_progress_recorded_during_a_stall_stop_leaves_the_record_for_the_next_tick(world):
    dreaming(world)
    world.table[0] = processes.ProcessTable([row(4100)])
    world.clock[0] += timedelta(minutes=11)
    stop = world.ctx.kill_runner

    def kill_while_progressing(pid):
        world.store.update_run(KEY, last_progress_at=world.clock[0].isoformat())
        stop(pid)

    world.ctx.kill_runner = kill_while_progressing
    recover(world)
    run = world.store.get_dream("orch")
    assert run.status == "running" and world.removed == [] and run.restarts == 0




def test_every_dream_starts_its_branch_from_the_base(tmp_path):
    adds = []
    repo = SimpleNamespace(add_worktree=lambda path, branch, ref: adds.append((branch, ref)), at=lambda path: repo)
    ctx = SimpleNamespace(git=repo, config=Config(repo="o/r", base_branch="trunk", lanes={"orch": {"paths": ["orch/**"]}}))
    orch_dream_worker._dream_worktree(ctx, "orch", tmp_path / "w")
    assert adds == [("foragent/dream-orch", "origin/trunk")]


def publish_git(remote):
    pushes = []
    listing = f"{remote}\trefs/heads/foragent/dream-orch\n" if remote else ""
    git = SimpleNamespace(push=lambda branch, lease=None: pushes.append((branch, lease)),
                          _call=lambda args: listing if args[:1] == ["ls-remote"] else "")
    return git, pushes


def test_publish_with_an_open_dream_pr_pushes_nothing_and_ends_the_dream(world):
    git, pushes = publish_git("old-head")
    world.ctx.tracker = SimpleNamespace(find_pr=lambda branch: 12, create_pr=lambda **kw: pytest.fail("no PR"))
    with pytest.raises(RuntimeError, match="#12"):
        orch_dream_worker._publish(world.ctx, git, "orch", MEMORY, 1)
    assert pushes == []


@pytest.mark.parametrize("remote", ["closed-pr-head", None], ids=["existing-branch", "no-branch"])
def test_publish_force_pushes_leased_on_the_remote_head_it_read(world, remote):
    git, pushes = publish_git(remote)
    world.ctx.tracker = SimpleNamespace(find_pr=lambda branch: None, create_pr=lambda **kw: 31)
    assert orch_dream_worker._publish(world.ctx, git, "orch", MEMORY, 1) == 31
    assert pushes == [("foragent/dream-orch", remote or "")]


def tick_start(world, monkeypatch, open_prs, lanes=("orch", "view")):
    from foragent import scheduler as facade

    world.config = Config(repo="o/r", lanes={name: {"paths": [f"{name}/**"]} for name in lanes})
    world.ctx.config = world.config
    order = []
    monkeypatch.setattr(facade, "_has_slot", lambda ctx, budget: True)
    monkeypatch.setattr(orch_dream, "dream_due", lambda *args: list(lanes))

    @contextmanager
    def lock(store):
        order.append("lock")
        yield

    monkeypatch.setattr(facade, "machine_lock", lock)

    def find_pr(branch):
        order.append(branch)
        result = open_prs.get(branch)
        if isinstance(result, Exception):
            raise result
        return result

    world.ctx.tracker = SimpleNamespace(find_pr=find_pr)
    world.ctx.launcher = lambda **kwargs: 5100
    report = SimpleNamespace(actions=[])
    orch_dream.start(world.ctx, list(lanes), 2, report)
    return order, report


def test_tick_skips_a_lane_with_an_open_dream_pr_and_starts_the_next(world, monkeypatch):
    order, _ = tick_start(world, monkeypatch, {"foragent/dream-orch": 40})
    assert order == ["foragent/dream-orch", "foragent/dream-view", "lock"], "lookups come before the lock"
    assert world.store.get_dream("orch") is None and world.store.get_dream("view").status == "running"


def test_tick_starts_nothing_when_every_due_lane_has_an_open_dream_pr(world, monkeypatch):
    order, _ = tick_start(world, monkeypatch, {"foragent/dream-orch": 40, "foragent/dream-view": 41})
    assert order == ["foragent/dream-orch", "foragent/dream-view"] and world.store.active_runs() == []


@pytest.mark.parametrize("rate_limit", [True, False])
def test_lookup_failure_starts_no_dream_and_a_rate_limit_pauses_github(world, monkeypatch, rate_limit):
    from foragent import scheduler as facade
    from foragent.tracker import TrackerError

    error = TrackerError("API rate limit exceeded" if rate_limit else "boom")
    monkeypatch.setattr(type(error), "is_rate_limit", property(lambda self: rate_limit))
    paused = []
    monkeypatch.setattr(facade.orch_github, "pause", lambda ctx, exc: paused.append(exc))
    order, _ = tick_start(world, monkeypatch, {"foragent/dream-orch": error})
    assert order == ["foragent/dream-orch"] and world.store.active_runs() == []
    assert paused == ([error] if rate_limit else [])


def test_dream_after_a_failed_pr_creation_starts_from_the_base_and_publishes_only_new_edits(world, monkeypatch):
    def failing():
        raise RuntimeError("PR creation failed after the push")

    def edit(worktree):
        path = worktree / MEMORY
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("a lesson\n")

    calls, publish = worker_world(world, edit, failing)
    monkeypatch.setattr(orch_dream_worker, "_publish", publish)
    dreaming(world, pid=None, start=None)
    with pytest.raises(RuntimeError):
        orch_dream_worker.run_dream("orch", world.ctx)
    assert world.store.get_dream("orch").outcome == "error" and orch_dream.last_dream_at(world.store, "orch") is None
    calls, publish = worker_world(world, lambda worktree: None, lambda: pytest.fail("nothing changed"))
    monkeypatch.setattr(orch_dream_worker, "_publish", publish)
    dreaming(world, pid=None, start=None)
    assert orch_dream_worker.run_dream("orch", world.ctx) == 0  # Base start, no edit: no GitHub call.
    assert calls["published"] == [] and world.store.get_dream("orch").outcome == "dreamed"


def main_lane_config():
    return Config(repo="o/r", executor="claude",
                  lanes={"main": {"paths": ["**"], "executor": "codex"}, "orch": {"paths": ["orch/**"]}})


def test_a_configured_main_lane_dreams_with_its_own_executor(world, monkeypatch):
    chosen = []

    class Executor:
        def run(self, **kwargs):
            from foragent.executors import ExecResult
            return ExecResult(0, False, "done", kwargs["log_path"])

    world.config = world.ctx.config = main_lane_config()
    calls, _ = worker_world(world, lambda worktree: None, lambda: 1)
    world.ctx.executor_factory = lambda name: chosen.append(name) or Executor()
    world.store.start_dream("main", worktree=world.store.root / "worktrees/dream-main")
    world.store.update_run("dream-main", pid=None)
    assert orch_dream_worker.run_dream("main", world.ctx) == 0
    assert chosen == ["codex"], "lanes.main.executor wins over the global executor"
    assert orch_dream.dream_config(world.config, "orch").executor == "claude"


def test_a_configured_main_lane_waits_on_its_own_executors_pause(world, monkeypatch):
    world.ctx.config = main_lane_config()
    monkeypatch.setattr(orch_dream, "dream_due", lambda *args: ["main", "orch"])
    world.store.pause_executor("codex", world.clock[0] + timedelta(hours=1))
    assert orch_dream._admissible(world.ctx) == ["orch"], "main waits on codex; orch's claude is free"


def unreadable_table(monkeypatch):
    def refuse():
        raise PermissionError("Process table unavailable; ownership cannot be verified")

    monkeypatch.setattr(processes, "list_processes", refuse)


def test_manual_completion_with_an_unreadable_process_table_records_the_event(world, monkeypatch):
    run = dreaming(world)
    world.alive[4100] = True
    unreadable_table(monkeypatch)
    assert orch_dream.dreamed_command(world.store, world.config, "orch") == 0
    assert orch_dream.last_dream_at(world.store, "orch") is not None
    assert world.store.get_dream("orch") == run


@pytest.mark.parametrize("minutes", [0, 11], ids=["fresh", "past-the-stall-limit"])
def test_recovery_with_an_unreadable_process_table_neither_finishes_nor_kills(world, monkeypatch, minutes):
    dreaming(world)
    world.alive[4100] = True
    world.clock[0] += timedelta(minutes=minutes)
    unreadable_table(monkeypatch)
    recover(world)
    run = world.store.get_dream("orch")
    assert run.status == "running" and world.removed == []
