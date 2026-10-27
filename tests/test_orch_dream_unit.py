"""Boundary tests for lane dreams (#189): process ownership, admission, inputs and publication."""

from datetime import UTC, datetime, timedelta
import json
import os
from types import SimpleNamespace

import pytest

from foragent import orch_dream, orch_dream_stop, orch_dream_worker, processes
from foragent.config import Config
from foragent.state import StateStore

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def row(pid, start=1.0, argv=None, uid=None):
    return SimpleNamespace(pid=pid, start_time=start, argv=argv, ppid=1, pgid=pid, sid=pid, uid=uid)


@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    config = Config(repo="o/r", lanes={"orch": {"paths": ["orch/**"]}}, stall_minutes=10, max_runtime_minutes=60)
    killed, removed = [], []

    def kill(pid):
        assert processes._recorded_tree.get() is not None, "a stop needs recorded ownership"
        killed.append(pid)

    ctx = SimpleNamespace(store=store, config=config, now=lambda: clock[0], kill_runner=kill,
                          git=SimpleNamespace(remove_worktree=removed.append))
    table = [processes.ProcessTable([])]
    monkeypatch.setattr(processes, "list_processes", lambda: table[0])
    alive = {}
    monkeypatch.setattr("foragent.scheduler_stop._pid_exists", lambda pid: alive.get(pid, False))
    return SimpleNamespace(store=store, config=config, ctx=ctx, clock=clock, killed=killed,
                           removed=removed, table=table, alive=alive)


def dreaming(world, *, pid=4100, identities=True, executor=None):
    world.store.start_dream("orch", worktree=world.store.root / "worktrees/dream-orch")
    fields = {"pid": pid, "executor_pgid": executor}
    if identities:
        fields["process_identities"] = [{"pid": pid, "start_time": 1.0}]
    world.store.update_dream("orch", **fields)
    return world.store.get_dream("orch")


def test_unreadable_worker_is_unknown_not_dead(world):
    run = dreaming(world)
    world.table[0] = processes.ProcessTable([], unreadable=[4100])
    assert orch_dream_stop.dream_liveness(run) is None
    report = SimpleNamespace(actions=[])
    orch_dream.recover(world.ctx, run, report)
    assert world.store.get_dream("orch").status == "running" and world.removed == []


def test_manual_completion_refuses_only_a_confirmed_live_worker(world):
    run = dreaming(world)
    world.table[0] = processes.ProcessTable([row(4100)])  # Confirmed alive: same pid and start time.
    assert orch_dream.dreamed_command(world.store, world.config, "orch") == 2
    assert orch_dream.last_dream_at(world.store, "orch") is None
    world.table[0] = processes.ProcessTable([], unreadable=[4100])  # Unknown: event only.
    assert orch_dream.dreamed_command(world.store, world.config, "orch") == 0
    assert orch_dream.last_dream_at(world.store, "orch") is not None
    assert world.store.get_dream("orch") == run, "the record belongs to the worker and the tick"


@pytest.mark.parametrize("table", [
    processes.ProcessTable([row(4500, argv=None)]),  # Our own process whose argv cannot be read.
    processes.ProcessTable([], unreadable=[4500]),  # An unreadable process of unknown owner.
], ids=["unreadable-argv", "unreadable-process"])
def test_pidless_dream_is_not_finished_while_a_worker_might_be_unreadable(world, table):
    from foragent import scheduler

    dreaming(world, pid=None, identities=False)
    world.table[0] = table
    world.clock[0] += scheduler.LAUNCH_GRACE + timedelta(seconds=1)
    orch_dream.recover(world.ctx, world.store.get_dream("orch"), SimpleNamespace(actions=[]))
    assert world.store.get_dream("orch").status == "running" and world.removed == []


def test_pidless_dream_with_every_process_read_and_no_worker_is_finished(world):
    from foragent import scheduler

    dreaming(world, pid=None, identities=False)
    other = os.getuid() + 1
    world.table[0] = processes.ProcessTable([row(4500, argv=("python", "other"), uid=os.getuid())],
                                            unreadable=[4600], unreadable_uids={4600: other})
    world.clock[0] += scheduler.LAUNCH_GRACE + timedelta(seconds=1)
    orch_dream.recover(world.ctx, world.store.get_dream("orch"), SimpleNamespace(actions=[]))
    assert world.store.get_dream("orch").outcome == "error" and world.removed


def test_refused_stop_with_an_unreadable_recorded_process_keeps_the_record(world):
    dreaming(world, identities=False)
    world.table[0] = processes.ProcessTable([], unreadable=[4100])
    world.ctx.kill_runner = processes.kill_process_group  # No recorded identity: the stop raises.
    run = world.store.get_dream("orch")
    assert orch_dream_stop._stop(world.ctx, run, SimpleNamespace(actions=[])) is False


def test_stalled_dream_stops_worker_and_executor_with_recorded_ownership(world):
    dreaming(world, executor=4200)
    world.table[0] = processes.ProcessTable([row(4100)])
    world.clock[0] += timedelta(minutes=11)
    report = SimpleNamespace(actions=[])
    orch_dream.recover(world.ctx, world.store.get_dream("orch"), report)
    assert world.killed == [4100, 4200]
    finished = world.store.get_dream("orch")
    assert finished.status == "finished" and finished.outcome == "error" and "stall" in finished.detail


def test_dead_worker_stops_surviving_executor_before_finishing(world):
    dreaming(world, executor=4200)
    report = SimpleNamespace(actions=[])
    orch_dream.recover(world.ctx, world.store.get_dream("orch"), report)
    assert world.killed == [4100, 4200], "the surviving executor tree is stopped first"
    assert world.store.get_dream("orch").outcome == "error"


def test_refused_stop_of_surviving_executor_keeps_the_record(world, monkeypatch):
    dreaming(world, identities=False, executor=4200)
    world.alive[4200] = True  # The executor group survives; no recorded identity authorizes a stop.
    world.ctx.kill_runner = processes.kill_process_group
    report = SimpleNamespace(actions=[])
    orch_dream.recover(world.ctx, world.store.get_dream("orch"), report)
    assert world.store.get_dream("orch").status == "running"
    assert any("stop refused" in action for action in report.actions)


def test_worker_records_executor_tree_for_later_stops(world, tmp_path, monkeypatch):
    seen = []

    class Executor:
        def run(self, **kwargs):
            processes._process_tree_callback.get()([{"pid": 4300, "start_time": 2.0}])
            seen.append(world.store.get_dream("orch").process_identities)
            raise RuntimeError("stop here")

    dreaming(world)
    world.ctx.git = SimpleNamespace(remove_worktree=lambda path: None, add_worktree=lambda *a: None,
                                    at=lambda path: None)
    world.ctx.executor_factory = lambda name: Executor()
    world.ctx.poll_hook = lambda execute, poll: execute()
    with pytest.raises(RuntimeError):
        orch_dream_worker.run_dream("orch", world.ctx)
    assert {"pid": 4300, "start_time": 2.0} in seen[0]
    assert {"pid": 4100, "start_time": 1.0} in seen[0]


def test_start_rechecks_a_lane_whose_issue_started_this_tick(world, monkeypatch):
    from foragent import scheduler

    monkeypatch.setattr(scheduler, "_has_slot", lambda ctx, budget: True)
    monkeypatch.setattr(orch_dream, "dream_due", lambda *args: [])  # An issue of orch started meanwhile.
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


def test_open_dream_pr_keeps_its_edits_and_is_never_force_pushed(world):
    calls = []
    git = SimpleNamespace(
        fetch=lambda remote, ref: calls.append(("fetch", ref)),
        _call=lambda args: calls.append(("git", args)) or "abc123\n",
        push=lambda branch, lease=None: calls.append(("push", branch, lease)),
    )
    updated = []
    world.ctx.tracker = SimpleNamespace(find_pr=lambda branch: 9,
                                        update_pr=lambda pr, **kwargs: updated.append(pr))
    assert orch_dream_worker._publish(world.ctx, git, "orch", ".claude/agent-memory/orch/MEMORY.md", 2) == 9
    merges = [args for kind, *rest in calls if kind == "git" for args in rest if "merge" in args]
    assert merges and "refs/remotes/origin/foragent/dream-orch" in merges[0]
    assert ("push", "foragent/dream-orch", "abc123") in calls and updated == [9]


def test_dream_done_event_names_the_pr_and_reference(world):
    event = orch_dream.record_done(world.store, "orch", 3, "ko", pr=12)
    saved = json.loads((world.store.root / "orch-events.jsonl").read_text().splitlines()[-1])
    assert saved["pr"] == 12 and "references/dreams.md" in event["summary"]
    assert event["say"] == "orch 레인이 꿈을 꿨어요 — 기억 3개 정리"


def test_identity_updates_add_to_the_record_instead_of_replacing_it(world):
    dreaming(world)
    world.store.update_dream("orch", process_identities=[{"pid": 4300, "start_time": 2.0}])  # Worker's executor.
    world.store.update_dream("orch", pid=4100, process_identities=[{"pid": 4100, "start_time": 1.0}])  # Launcher.
    assert world.store.get_dream("orch").process_identities == [
        {"pid": 4100, "start_time": 1.0}, {"pid": 4300, "start_time": 2.0}]


def test_start_rechecks_quota_exhausted_after_the_candidates(world, monkeypatch):
    from foragent import scheduler

    monkeypatch.setattr(scheduler, "_has_slot", lambda ctx, budget: True)
    monkeypatch.setattr(orch_dream, "dream_due", lambda *args: ["orch"])
    world.store.record_usage("claude", [{"name": "5h", "used_percent": 100,
                                         "resets_at": (NOW + timedelta(hours=1)).isoformat()}])
    started = []
    world.ctx.launcher = lambda **kwargs: started.append(kwargs)
    orch_dream.start(world.ctx, ["orch"], 2, SimpleNamespace(actions=[]))
    assert started == [] and world.store.get_dream("orch") is None


def test_second_dream_starts_from_the_dream_branch_with_the_base_merged(tmp_path):
    from foragent.git import GitError

    def fake(exists):
        calls = []

        def call(args):
            calls.append(args)
            if args[:3] == ["rev-parse", "--verify", "--quiet"] and not exists:
                raise GitError("missing")
            return ""

        repo = SimpleNamespace(_call=call, add_worktree=lambda path, branch, ref: calls.append(("add", branch, ref)),
                               at=lambda path: repo)
        return repo, calls

    for exists, start in ((False, "origin/main"), (True, "refs/remotes/origin/foragent/dream-orch")):
        repo, calls = fake(exists)
        ctx = SimpleNamespace(git=repo, config=Config(repo="o/r", lanes={"orch": {"paths": ["orch/**"]}}))
        orch_dream_worker._dream_worktree(ctx, "orch", tmp_path / "w")
        assert ("add", "foragent/dream-orch", start) in calls
        merged = [args for args in calls if isinstance(args, list) and "merge" in args]
        assert merged == ([[*orch_dream_worker.GIT_IDENTITY, "merge", "--no-edit", "origin/main"]] if exists else [])


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
    orch_dream_worker._publish = lambda ctx, git, lane, memory, count: published.append(count) or 7
    try:
        dreaming(world)
        assert orch_dream_worker.run_dream("orch", world.ctx) == 0
    finally:
        orch_dream_worker._publish = original
    assert committed == [[".claude/agent-memory/orch"]] and published == [2]
    saved = json.loads((world.store.root / "orch-events.jsonl").read_text().splitlines()[-1])
    assert saved["memories"] == 2 and saved["pr"] == 7
    assert not worktree.exists()
