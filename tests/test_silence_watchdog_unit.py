"""Boundary cases of the shared silence watchdog (#435); fakes only, no real signal."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from foragent import processes, run_baseline, run_baseline_plugins, scheduler  # noqa: F401  (scheduler first, as production imports it)
from foragent import scheduler_recovery, scheduler_silence
from foragent.config import Config
from foragent.run_waits import _budget_left, _remaining
from foragent.watchdog import SilenceWatchdog
from process_fakes import fake_process_table  # noqa: F401


@dataclass(frozen=True)
class Row:
    pid: int
    start_time: float
    cpu_time: float | None


RECORDED = [{"pid": 10, "start_time": 1.0}]


# Unknown-as-zero: an unreadable table or CPU is unknown, never silence.
@pytest.mark.parametrize("snapshot", [None, (Row(10, 1.0, None),)], ids=["table", "row"])
def test_unknown_cpu_is_never_silence_only_the_safety_net_stops(snapshot):
    watch = SilenceWatchdog(safety_net=5000)
    watch.observe(now=0, output_bytes=0, progress_events=0, snapshot=snapshot, identities=RECORDED)
    late = watch.observe(now=4000, output_bytes=0, progress_events=0, snapshot=snapshot, identities=RECORDED)
    assert not late.stop and not late.cpu_known
    net = watch.observe(now=5000, output_bytes=0, progress_events=0, snapshot=snapshot, identities=RECORDED)
    assert net.stop and net.decision == "safety-net"


def test_known_flat_cpu_is_still_silence():
    watch, rows = SilenceWatchdog(), (Row(10, 1.0, 3.0),)
    watch.observe(now=0, output_bytes=0, progress_events=0, snapshot=rows, identities=RECORDED)
    assert watch.observe(now=600, output_bytes=0, progress_events=0, snapshot=rows, identities=RECORDED).stop


class Process:
    """Popen.wait semantics over a fake clock; writes output at the given times."""

    def __init__(self, clock, output, writes, exit_at):
        self.clock, self.output, self.writes, self.exit_at = clock, output, list(writes), exit_at

    def wait(self, timeout=None):
        until = self.exit_at if timeout is None else min(self.exit_at, self.clock[0] + timeout)
        while self.writes and self.writes[0] <= until:
            self.clock[0] = self.writes.pop(0)
            self.output.write(b"tests/test_a.py::test_x PASSED\n")
            self.output.flush()
        self.clock[0] = until
        if until < self.exit_at:
            raise subprocess.TimeoutExpired("cmd", timeout)
        return 0


def _watch(monkeypatch, tmp_path, writes, exit_at, listing):
    clock = [0.0]
    monkeypatch.setattr(processes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(processes, "list_processes", listing)
    watch = SilenceWatchdog()
    with (tmp_path / "out").open("w+b") as output, (tmp_path / "log").open("ab") as log:
        stopped = processes._watch(Process(clock, output, writes, exit_at), watch, output, log,
                                   lambda: None, lambda: RECORDED)
    return stopped, watch, clock[0]


# Final-sample omission: a result written after the last poll is still a result.
def test_completion_keeps_the_last_result_gap(monkeypatch, tmp_path):
    stopped, watch, _ = _watch(monkeypatch, tmp_path, [5, 100], 100, lambda: (Row(10, 1.0, 0.0),))
    assert not stopped
    assert watch.result_gaps == (5, 95)


def test_unreadable_table_does_not_stop_a_cpu_only_command(monkeypatch, tmp_path):
    def unreadable():
        raise PermissionError("Process table unavailable")

    stopped, _, now = _watch(monkeypatch, tmp_path, [], 1500, unreadable)
    assert not stopped and now == 1500


# Output-format assumption: passes are recorded at -q and with spaces in ids.
def test_results_plugin_records_passes_without_verbose_output(tmp_path):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "tests" / "test_a.py").write_text(
        "import pytest\n"
        "def test_pass(): pass\n"
        "def test_fail(): assert False\n"
        "def test_skip(): pytest.skip('no')\n"
        "@pytest.fixture\n"
        "def broken():\n    yield\n    raise RuntimeError('teardown')\n"
        "def test_teardown_error(broken): pass\n"
        "@pytest.mark.parametrize('value', ['a b'])\n"
        "def test_param(value): pass\n"
    )
    plugins, listing = tmp_path / "plugins", tmp_path / "passes.jsonl"
    plugins.mkdir()
    (plugins / "_foragent_passes_test.py").write_text(run_baseline_plugins.RESULTS_PLUGIN.format(listing=str(listing)))
    command = run_baseline._with_python_path(
        f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider tests -p _foragent_passes_test",
        str(plugins))
    subprocess.run(["/bin/sh", "-c", command], cwd=project, capture_output=True, check=False)
    assert run_baseline_plugins._plugin_passes(listing, project) == {
        "tests/test_a.py::test_pass", "tests/test_a.py::test_param[a b]",
    }


def test_streamed_verbose_line_keeps_ids_with_spaces(tmp_path):
    log = tmp_path / "log"
    log.write_text("tests/test_a.py::test_param[a b] PASSED            [ 50%]\n"
                   "tests/test_a.py::test_other FAILED\n")
    assert run_baseline_plugins._reported_passes(tmp_path / "missing.xml", log, 0) == {"tests/test_a.py::test_param[a b]"}


# Fixed deadline: max_runtime_minutes never bounds a run's own budget.
def test_runner_budget_ignores_max_runtime_and_outlasts_the_gate_safety_net():
    started = datetime(2026, 10, 29, tzinfo=UTC)
    run = SimpleNamespace(started_at=started.isoformat(), waited_seconds=0, slept_seconds=0)
    ctx = SimpleNamespace(store=SimpleNamespace(get_run=lambda n: run), now=lambda: started,
                          config=Config(max_runtime_minutes=2), deadline_seconds=None)
    assert _budget_left(1, ctx) > 24 * 3600, "The gate's resumable safety-net stop must come first"
    assert _remaining(1, ctx) == 120, "A session's budget is still max_runtime_minutes, applied only once quiet"
    ctx.deadline_seconds = 60  # A probe (fa doctor) keeps its own deadline.
    assert _budget_left(1, ctx) == _remaining(1, ctx) == 60


# Restart policy: silence and safety-net stops always restart, whatever the count.
@pytest.mark.parametrize("decision", ["safety-net", "silent"])
def test_watchdog_stop_restarts_even_after_an_earlier_stall(monkeypatch, decision):
    calls = []
    def kill(*args, **kwargs):
        calls.append(("kill", kwargs))
        kwargs["on_stopped"]()  # The processes ended.
        return True

    monkeypatch.setattr(scheduler_recovery.scheduler, "_kill_run", kill)
    monkeypatch.setattr(scheduler_recovery.scheduler, "_restore_stalled",
                        lambda ctx, number, restarts, report: calls.append(("restore", restarts)))
    monkeypatch.setattr(scheduler_silence, "_log", lambda *args: calls.append(("log", args[3])))
    fact = SilenceWatchdog().observe(now=0, output_bytes=0, progress_events=0, snapshot=(), identities=())
    fact = replace(fact, stop=True, decision=decision)
    run = SimpleNamespace(number=7, key=7, kind="issue", restarts=1)
    assert scheduler_silence.stop(object(), run, SimpleNamespace(actions=[]), fact, past_limit=False, wake_grace=False)
    kill = calls[0][1]
    assert kill["outcome"] == "stalled-restart" and kill["count_restart"] is False
    assert ("restore", 1) in calls
    assert calls.count(("log", "safety-net" if decision == "safety-net" else "silence-stop")) == 1


def test_interrupted_watchdog_stop_finishes_as_restart(monkeypatch):
    restored = []
    finished = SimpleNamespace(outcome="stalled-restart", restarts=3)
    run = SimpleNamespace(number=7, key=7, kind="issue", lane=None, status="running", pid=None,
                          process_identities=[], last_progress_at=None, outcome="stalled-restart",
                          executor_pgid=None, worktree="/w", detail="")
    store = SimpleNamespace(finish_run=lambda *args, **kwargs: finished, update_run=lambda *a, **k: None,
                            get_run=lambda key: run)
    ctx = SimpleNamespace(store=store)
    monkeypatch.setattr(scheduler_recovery.scheduler, "_remove_run_worktree", lambda *args, **kwargs: None)
    monkeypatch.setattr(scheduler_recovery.scheduler, "_restore_stalled",
                        lambda ctx, number, restarts, report: restored.append(restarts))

    scheduler_recovery._finish_dead_run(ctx, run, SimpleNamespace(actions=[]))
    assert restored == [1]


# A silent tree's stop ends its whole snapshot tree with that one listing (#435).
def test_quiescent_stop_ends_the_snapshot_tree_with_one_listing(fake_process_table, monkeypatch, tmp_path):
    table = fake_process_table
    root = table.add(42000, cwd=tmp_path, start_time=10)
    child = table.add(42001, ppid=root.pid, cwd=tmp_path, start_time=11)
    listings = []
    listing = table.list_processes
    monkeypatch.setattr(processes, "list_processes", lambda: listings.append(1) or listing())

    def read(pid):
        try:
            return table.process(pid)
        except ProcessLookupError:
            return None

    monkeypatch.setattr(processes, "read_process_identity", read)
    # A grandchild the snapshot witnessed but nobody recorded yet: the silent stop ends it too.
    grandchild = table.add(45000, ppid=child.pid, cwd=tmp_path, start_time=50)
    recorded = [{"pid": row.pid, "start_time": row.start_time} for row in (root, child)]
    with processes.process_snapshot():
        with processes.quiescent_tree(), processes.recorded_tree(recorded):
            processes.kill_process_group(root.pid)
    assert grandchild.pid not in table.live
    assert not {root.pid, child.pid} & table.live
    assert len(listings) == 1, "The silent stop reuses the tick snapshot with targeted reads, no other scan"


# Out of scope (CI polling, merge turns): waiting loops keep max_runtime_minutes as their bound.
def test_waiting_loops_keep_the_configured_runtime_bound():
    started = datetime(2026, 10, 29, tzinfo=UTC)
    run = SimpleNamespace(started_at=started.isoformat(), waited_seconds=0, slept_seconds=0)
    ctx = SimpleNamespace(store=SimpleNamespace(get_run=lambda n: run), now=lambda: started,
                          config=Config(max_runtime_minutes=2), deadline_seconds=None)
    assert _remaining(1, ctx) == 120
    assert _budget_left(1, ctx) > 24 * 3600, "Only waiting is bounded; step boundaries use the safety net"
    ctx.config = Config()  # The default 90 minutes still bound waiting, not the safety net.
    assert _remaining(1, ctx) == 90 * 60


# A dream run shares the stop path by record key: it ends as error and touches no labels.
@pytest.mark.parametrize("decision", ["safety-net", "silent"])
def test_watchdog_stop_of_a_dream_ends_as_error_without_labels(monkeypatch, decision):
    calls = []
    def kill(ctx, key, report, **kwargs):
        calls.append(("kill", key, kwargs))
        kwargs["on_stopped"]()
        return True

    monkeypatch.setattr(scheduler_recovery.scheduler, "_kill_run", kill)
    monkeypatch.setattr(scheduler_recovery.scheduler, "_restore_stalled",
                        lambda *args: calls.append(("restore",)))
    monkeypatch.setattr(scheduler_recovery.scheduler, "append_event",
                        lambda store, at, number, kind, summary, **facts: calls.append(("event", number, kind)))
    fact = SilenceWatchdog().observe(now=0, output_bytes=0, progress_events=0, snapshot=(), identities=())
    fact = replace(fact, stop=True, decision=decision)
    run = SimpleNamespace(number=0, key="dream-orch", kind="dream", lane="orch", restarts=0)
    ctx = SimpleNamespace(store=None, now=lambda: datetime(2026, 10, 29, tzinfo=UTC))
    assert scheduler_silence.stop(ctx, run, SimpleNamespace(actions=[]), fact, past_limit=False, wake_grace=False)
    kill = calls[0]
    assert kill[1] == "dream-orch" and kill[2]["outcome"] == "error" and kill[2]["count_stall"] is False
    assert kill[2]["decided"] is run
    assert ("restore",) not in calls
    assert [call for call in calls if call[0] == "event"] == [
        ("event", None, "safety-net" if decision == "safety-net" else "silence-stop")]


def _quiescent_case(table, monkeypatch, tmp_path):
    def read(pid):
        try:
            return table.process(pid)
        except ProcessLookupError:
            return None

    monkeypatch.setattr(processes, "read_process_identity", read)
    return read


# Unsafe traversal root: a recorded protected process never adopts its children.
@pytest.mark.parametrize("protected", ["init", "parent"])
def test_quiescent_stop_never_traverses_from_a_protected_process(fake_process_table, monkeypatch, tmp_path, protected):
    table = fake_process_table
    _quiescent_case(table, monkeypatch, tmp_path)
    pid = 1 if protected == "init" else table.parent_pid
    unrelated = table.add(46000, ppid=pid, cwd=tmp_path, start_time=50)
    recorded = [{"pid": pid, "start_time": table.rows[pid].start_time}]
    with processes.process_snapshot(), processes.quiescent_tree():
        found = processes.observe_tree(recorded)
    assert unrelated.pid not in {item["pid"] for item in found}


# Unverified parent: a PID reused between the two parent reads adopts nothing.
def test_quiescent_stop_ignores_children_of_a_reused_parent_pid(fake_process_table, monkeypatch, tmp_path):
    table = fake_process_table
    read = _quiescent_case(table, monkeypatch, tmp_path)
    root = table.add(42000, cwd=tmp_path, start_time=10)
    table.add(47000, ppid=root.pid, cwd=tmp_path, start_time=95)
    reads = []

    def reused(pid):
        reads.append(pid)
        if pid == root.pid and reads.count(pid) == 2:  # The PID is reused right after the first read.
            table.exit(root.pid)
            table.add(root.pid, cwd=tmp_path, start_time=90)
        return read(pid)

    with processes.process_snapshot():
        monkeypatch.setattr(processes, "read_process_identity", reused)
        with processes.quiescent_tree():
            found = processes.observe_tree([{"pid": root.pid, "start_time": 10}])
    assert 47000 not in {item["pid"] for item in found}


# Unknown as known: a recorded pid the table could not read is unknown CPU, never silence.
def test_unreadable_recorded_child_is_unknown_cpu():
    table = processes.ProcessTable([Row(10, 1.0, 0.0)], unreadable={11})
    identities = [*RECORDED, {"pid": 11, "start_time": 2.0}]
    watch = SilenceWatchdog()
    watch.observe(now=0, output_bytes=0, progress_events=0, snapshot=table, identities=identities)
    late = watch.observe(now=600, output_bytes=0, progress_events=0, snapshot=table, identities=identities)
    assert not late.cpu_known and not late.stop


# Run-ending check stopped by the watchdog: restart, not timeout/blocked.
def test_pr_head_check_stopped_by_the_watchdog_restarts(monkeypatch, tmp_path):
    from foragent import run_checks, runner
    from foragent.issue import ParsedIssue
    from foragent.run_attempt import ExecutorStopped
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state")
    store.start_run(3, branch="b", worktree=tmp_path / "w")
    ctx = SimpleNamespace(store=store, config=Config(), now=lambda: datetime.now(UTC),
                          repo_root=tmp_path, deadline_seconds=None)
    monkeypatch.setattr(runner, "_check", lambda command, *a, **k: run_checks.CheckResult(command, -15, True, ""))
    restarts = []

    def restart(n, ctx, detail):
        restarts.append(detail)
        raise ExecutorStopped("stalled-restart", detail)

    monkeypatch.setattr(run_checks, "watchdog_restart", restart)
    monkeypatch.setattr(run_checks, "_timeout", lambda n, ctx: pytest.fail("A watchdog stop is not a timeout"))
    with pytest.raises(ExecutorStopped, match="watchdog"):
        run_checks._check_review_head(3, ctx, SimpleNamespace(root=tmp_path), ParsedIssue(None, "", "", ["true"], []),
                                      "head", "", [])
    assert len(restarts) == 1


# Check site without history: the Stop hook sizes its window from the run's history.
def test_stop_hook_checks_use_the_repository_history_and_safety_net(tmp_path):
    from foragent import watchdog

    state = tmp_path / "data" / "owner__repo"
    snapshot = state / "approvals" / "1.json"
    cache = tmp_path / "data" / "baselines"
    cache.mkdir(parents=True)
    (cache / "a.json").write_text(json.dumps({
        "repository": str(tmp_path.resolve()), "created_at": "2026-10-28T00:00:00+00:00",
        "result_gaps_seconds": [120, 720]}))
    assert watchdog.hook_args(snapshot, tmp_path, 3600) == {"history_gaps": (120, 720), "safety_net": 3600}
    assert watchdog.hook_args(snapshot, None, None) == {"history_gaps": (), "safety_net": watchdog.SAFETY_NET_SECONDS}


# Setting lost at a process boundary: the detached builder keeps the configured safety net.
def test_detached_baseline_builder_keeps_the_configured_safety_net(monkeypatch, tmp_path):
    from foragent import run_baseline_process

    seen = []
    monkeypatch.setattr(run_baseline, "_build_baseline", lambda context, build, lock: seen.append(context.config))
    monkeypatch.setattr(run_baseline_process, "load_record", lambda cls, value: value)
    monkeypatch.setattr(run_baseline_process.os, "fdopen", lambda fd, mode: None)
    request = {"repo_root": str(tmp_path), "store_root": str(tmp_path / "state"), "acceptance_dir": "tests/acceptance/",
               "test_slots": 1, "machine_root": str(tmp_path / "machine"), "safety_net_hours": 1, "build": {}}
    run_baseline_process.main(["x", json.dumps(request), "9"])
    assert seen[0].safety_net_hours == 1


# Decision record lost to a race: the stop happened, so its facts are journaled.
def test_watchdog_decision_is_journaled_when_the_runner_finishes_during_the_stop(monkeypatch):
    events = []

    def kill(ctx, key, report, **kwargs):
        kwargs["on_stopped"]()  # The processes ended, then the runner's own finish won the record.
        return False

    monkeypatch.setattr(scheduler_recovery.scheduler, "_kill_run", kill)
    monkeypatch.setattr(scheduler_recovery.scheduler, "append_event",
                        lambda store, at, number, kind, summary, **facts: events.append((kind, facts["decision"])))
    fact = SilenceWatchdog().observe(now=0, output_bytes=0, progress_events=0, snapshot=(), identities=())
    run = SimpleNamespace(number=7, key=7, kind="issue", restarts=0)
    ctx = SimpleNamespace(store=None, now=lambda: datetime(2026, 10, 29, tzinfo=UTC))
    scheduler_silence.stop(ctx, run, SimpleNamespace(actions=[]), replace(fact, stop=True, decision="silent"),
                           past_limit=False, wake_grace=False)
    assert events == [("silence-stop", "silent")]


def test_gate_check_done_passes_the_hook_silence_inputs_only_when_given(monkeypatch, tmp_path):
    from foragent import cli

    calls = []
    monkeypatch.setattr(cli, "check_done", lambda *args, **kwargs: calls.append(kwargs) or 0)
    base = ["gate", "check-done", "--snapshot", str(tmp_path / "s.json"), "--worktree", str(tmp_path),
            "--acceptance-dir", "tests/acceptance/"]
    assert cli.main([*base, "--history-root", str(tmp_path), "--safety-net-seconds", "60"]) == 0
    assert cli.main(base) == 0
    assert calls == [{"history_root": tmp_path, "safety_net_seconds": 60}, {}]


# Fixed deadline on a session: past its budget it runs on while its CPU grows, then ends once quiet.
@pytest.mark.parametrize("busy_until,stopped,ends", [(1000, False, 1000), (500, True, 560)])
def test_session_past_its_budget_ends_only_once_quiet(monkeypatch, tmp_path, busy_until, stopped, ends):
    clock = [0.0]
    monkeypatch.setattr(processes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(processes, "list_processes", lambda: (Row(10, 1.0, min(clock[0], busy_until)),))
    with (tmp_path / "out").open("w+b") as output, (tmp_path / "log").open("ab") as log:
        result = processes._watch(Process(clock, output, [], 1000), processes._past_budget(), output, log,
                                  lambda: None, lambda: RECORDED)
    assert result is stopped and clock[0] == ends
    if stopped:
        assert json.loads((tmp_path / "log").read_text().splitlines()[-1])["decision"] == "runtime-limit"


def test_executor_session_reaching_its_budget_is_watched_not_killed(monkeypatch, tmp_path):
    from foragent.executors import ClaudeExecutor

    watched, waits = [], []

    class Session:
        pid = 54321
        returncode = 0

        def __init__(self, args, **kwargs):
            kwargs["stdout"].write(json.dumps({"result": "(none)", "is_error": False}).encode())

        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("claude", timeout)  # Still working at its budget.
            return 0

        def poll(self):
            return 0 if len(waits) > 1 else None

    def watch(process, watchdog, *args, **kwargs):
        watched.append(watchdog.runtime_limit)
        process.wait(timeout=5)
        return False  # It finished while growing.

    monkeypatch.setattr("foragent.processes.subprocess.Popen", Session)
    monkeypatch.setattr(processes, "_watch", watch)
    result = ClaudeExecutor().run(worktree=tmp_path, prompt="ignored", model=None, timeout_s=12,
                                  log_path=tmp_path / "review.log", review_level="high")
    assert waits[0] == 12 and watched == [0]
    assert not result.timed_out


# Inexact selection: --deselect matches prefixes, so a retained id that prefixes another stays selected.
def test_resume_deselects_only_ids_that_prefix_no_other_collected_id(tmp_path):
    from foragent.state import StateStore

    cache = StateStore(tmp_path / "baselines")
    cache.root.mkdir(parents=True)
    retained = {"tests/a.py::test_work", "tests/a.py::test_other"}
    run_baseline_plugins._retain_passes(cache, "k", retained, tmp_path / "log",
                                        {"tests/a.py::test_work", "tests/a.py::test_work_more", "tests/a.py::test_other"})
    assert run_baseline_plugins.retained_passes(cache, "k") == retained
    assert run_baseline_plugins.deselectable(cache, "k", retained) == {"tests/a.py::test_other"}


def test_results_plugin_records_the_whole_collection_including_deselected_ids(tmp_path):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "tests" / "test_a.py").write_text("def test_work(): pass\ndef test_work_more(): pass\n")
    plugins, listing = tmp_path / "plugins", tmp_path / "passes.jsonl"
    plugins.mkdir()
    (plugins / "_foragent_passes_test.py").write_text(run_baseline_plugins.RESULTS_PLUGIN.format(listing=str(listing)))
    command = run_baseline._with_python_path(
        f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider tests "
        "--deselect=tests/test_a.py::test_work_more -p _foragent_passes_test", str(plugins))
    subprocess.run(["/bin/sh", "-c", command], cwd=project, capture_output=True, check=False)
    passed, collected = run_baseline_plugins._plugin_records(listing, project)
    assert passed == {"tests/test_a.py::test_work"}
    assert collected == {"tests/test_a.py::test_work", "tests/test_a.py::test_work_more"}


# Missing measurement read as silence: a first sample judges by the last measured activity.
@pytest.mark.parametrize("last_activity,grew", [(1000.0, True), (0.0, False)])
def test_first_sample_judges_growth_by_its_last_measured_activity(last_activity, grew):
    watch = SilenceWatchdog(started=0.0)
    watch.last_activity_at = watch.last_output_at = last_activity  # Progress recorded just now, or long ago.
    assert watch.observe(now=1010, output_bytes=0, progress_events=0, snapshot=(), identities=()).grew is grew


# Omitted output channel: a session writing only to stderr is still producing output.
def test_session_writing_only_stderr_is_not_quiet(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(processes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(processes, "list_processes", lambda: (Row(10, 1.0, 0.0),))
    writes = [float(t) for t in range(5, 1000, 5)]
    with (tmp_path / "out").open("w+b") as output, (tmp_path / "err").open("w+b") as errors, \
            (tmp_path / "log").open("ab") as log:
        stopped = processes._watch(Process(clock, errors, writes, 1000), processes._past_budget(), output, log,
                                   lambda: None, lambda: RECORDED, channels=(errors,))
    assert stopped is False and clock[0] == 1000


# Stop reason lost in result handling: a safety-net stop restarts the run, a silent one stays a failure.
def test_check_result_carries_why_the_watchdog_stopped_it(monkeypatch, tmp_path):
    from foragent import run_checks, runner

    def run_logged(argv, *, watchdog, log_path, **kwargs):
        log_path.write_text("")
        watchdog.observe(now=0, output_bytes=0, progress_events=0, snapshot=(), identities=())
        watchdog.observe(now=watchdog.safety_net, output_bytes=1, progress_events=0, snapshot=(), identities=())
        return -15, True, ""

    monkeypatch.setattr(runner, "run_logged", run_logged)
    result = run_checks._check("pytest", tmp_path, tmp_path / "log", 900, safety_net=100)
    assert result.timed_out and result.stop == "safety-net"


@pytest.mark.parametrize("stop,restarted", [("safety-net", True), ("silent", False), (None, False)])
def test_after_check_restarts_only_at_the_safety_net(monkeypatch, stop, restarted):
    from foragent import run_checks, run_waits

    calls = []
    monkeypatch.setattr(run_waits, "_progress", lambda n, ctx: None)
    monkeypatch.setattr(run_checks, "save_failures", lambda *args, **kwargs: calls.append("saved"))
    monkeypatch.setattr(run_waits, "watchdog_restart", lambda n, ctx, detail: calls.append("restart"))
    ctx = SimpleNamespace(store=SimpleNamespace(root=Path("state")))
    run_waits.after_check(1, ctx, [run_checks.CheckResult("pytest", -15, stop is not None, "", stop=stop)])
    assert calls == (["saved", "restart"] if restarted else [])


# Dropped metadata: the premerge suite keeps the gaps the watchdog measured.
def test_premerge_suite_keeps_the_measured_result_gaps(monkeypatch, tmp_path):
    from contextlib import nullcontext

    from foragent import orch

    def run_logged(argv, *, watchdog, **kwargs):
        for now, written in ((0, 0), (30, 1), (90, 2)):
            watchdog.observe(now=now, output_bytes=written, progress_events=0, snapshot=(), identities=())
        return 0, False, ""

    monkeypatch.setattr(orch, "run_logged", run_logged)
    ctx = SimpleNamespace(store=SimpleNamespace(root=tmp_path / "state"), repo_root=tmp_path, config=Config())
    result = orch._suite_process(ctx, 5, "pytest", tmp_path, tmp_path / "pre.log", "head", nullcontext())
    assert result.result_gaps == (30, 60)
