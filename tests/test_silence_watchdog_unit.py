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
from foragent.run_waits import _remaining
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
    assert _remaining(1, ctx) > 24 * 3600, "The gate's resumable safety-net stop must come first"
    ctx.deadline_seconds = 120  # A probe (fa doctor) keeps its own deadline.
    assert _remaining(1, ctx) == 120


# Restart policy: silence and safety-net stops always restart, whatever the count.
@pytest.mark.parametrize("decision", ["safety-net", "silent"])
def test_watchdog_stop_restarts_even_after_an_earlier_stall(monkeypatch, decision):
    calls = []
    monkeypatch.setattr(scheduler_recovery.scheduler, "_kill_run",
                        lambda *args, **kwargs: calls.append(("kill", kwargs)) or True)
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


# Stale tree: a silent tree's stop still ends a child forked after the snapshot.
def test_quiescent_stop_ends_a_child_forked_after_the_snapshot(fake_process_table, monkeypatch, tmp_path):
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
    monkeypatch.setattr(processes, "read_children", lambda pid: tuple(
        row for number, row in table.rows.items() if number in table.live and row.ppid == pid))
    recorded = [{"pid": row.pid, "start_time": row.start_time} for row in (root, child)]
    with processes.process_snapshot():
        late = table.add(45000, ppid=child.pid, cwd=tmp_path, start_time=50)
        with processes.quiescent_tree(), processes.recorded_tree(recorded):
            processes.kill_process_group(root.pid)
    assert late.pid not in table.live
    assert not {root.pid, child.pid} & table.live
    assert len(listings) == 1, "The silent stop reuses the tick snapshot with targeted reads"


# Out of scope (CI polling, merge turns): waiting loops keep max_runtime_minutes as their bound.
def test_waiting_loops_keep_the_configured_runtime_bound():
    from foragent.run_waits import _wait_remaining

    started = datetime(2026, 10, 29, tzinfo=UTC)
    run = SimpleNamespace(started_at=started.isoformat(), waited_seconds=0, slept_seconds=0)
    ctx = SimpleNamespace(store=SimpleNamespace(get_run=lambda n: run), now=lambda: started,
                          config=Config(max_runtime_minutes=2), deadline_seconds=None)
    assert _wait_remaining(1, ctx) == 120
    assert _remaining(1, ctx) > 24 * 3600, "Only waiting is bounded; progressing work is not"
    ctx.config = Config()
    assert _wait_remaining(1, ctx) == _remaining(1, ctx)


# A dream run shares the stop path by record key: it ends as error and touches no labels.
@pytest.mark.parametrize("decision", ["safety-net", "silent"])
def test_watchdog_stop_of_a_dream_ends_as_error_without_labels(monkeypatch, decision):
    calls = []
    monkeypatch.setattr(scheduler_recovery.scheduler, "_kill_run",
                        lambda ctx, key, report, **kwargs: calls.append(("kill", key, kwargs)) or True)
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
