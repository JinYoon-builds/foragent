"""Boundary cases of the one silence decider (#435); fakes and fake clocks only, no real signal."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from foragent import processes, run_baseline_resume, scheduler  # noqa: F401  (scheduler first, as production imports it)
from foragent import run_checks, run_waits, scheduler_silence, watchdog
from foragent.config import Config
from foragent.watchdog import Facts, SilenceWatchdog, decide, outcome_for, stopped_check_kind


@dataclass(frozen=True)
class Row:
    pid: int
    start_time: float
    cpu_time: float | None


RECORDED = [{"pid": 10, "start_time": 1.0}]


def facts(**values):
    base = dict(silence_age=0, window_seconds=600, elapsed=0, safety_net=86400, cpu_known=True, runtime_limit=6000)
    return Facts(**{**base, **values})


# One decider: precedence safety-net > runtime-limit > silent, and runtime-limit needs the same silence.
@pytest.mark.parametrize("values,kind", [
    ({}, "running"),
    ({"silence_age": 600}, "silent"),
    ({"silence_age": 600, "elapsed": 6001}, "runtime-limit"),
    ({"silence_age": 599, "elapsed": 6001}, "running"),
    ({"silence_age": 600, "elapsed": 86400}, "safety-net"),
    ({"silence_age": 0, "elapsed": 86400}, "safety-net"),
    ({"silence_age": 9999, "elapsed": 6001, "cpu_known": False}, "running"),
])
def test_decide_follows_one_precedence(values, kind):
    assert decide(facts(**values)) == kind


def test_outcome_for_maps_each_stopping_kind():
    assert [outcome_for(kind) for kind in ("running", "silent", "runtime-limit", "safety-net")] == [
        None, "stalled-restart", "killed", "stalled-restart"]


@pytest.mark.parametrize("stop,past,kind", [
    (None, False, "silent"), ("silent", True, "runtime-limit"), ("safety-net", True, "safety-net")])
def test_a_stopped_check_is_decided_again_with_the_run_limit(stop, past, kind):
    assert stopped_check_kind(stop, past) == kind


# No earlier sample: the reference point is the start, growth is 0.
def test_first_observation_measures_silence_from_the_start():
    watch = SilenceWatchdog(started=0.0, runtime_limit=6000)
    first = watch.observe(now=6060, output_bytes=500, progress_events=0, snapshot=(Row(10, 1.0, 50.0),),
                          identities=RECORDED)
    assert (first.kind, first.outcome, first.decision, first.cpu_delta) == ("runtime-limit", "killed", "silent", 0)


# Unknown CPU is never silence: an unreadable table, an unreadable recorded pid or a failed CPU read.
@pytest.mark.parametrize("snapshot", [None, processes.ProcessTable([], unreadable={10}), (Row(10, 1.0, None),)],
                         ids=["table", "unreadable-pid", "cpu-read"])
def test_unknown_cpu_leaves_only_the_safety_net(snapshot):
    watch = SilenceWatchdog(safety_net=5000)
    watch.observe(now=0, output_bytes=0, progress_events=0, snapshot=snapshot, identities=RECORDED)
    late = watch.observe(now=4000, output_bytes=0, progress_events=0, snapshot=snapshot, identities=RECORDED)
    assert not late.stop and not late.cpu_known
    assert watch.observe(now=5000, output_bytes=0, progress_events=0, snapshot=snapshot,
                         identities=RECORDED).kind == "safety-net"


class Process:
    """Popen.wait over a fake clock; writes a result line at the given times."""

    def __init__(self, clock, stream, writes, exit_at):
        self.clock, self.stream, self.writes, self.exit_at = clock, stream, list(writes), exit_at

    def wait(self, timeout=None):
        until = self.exit_at if timeout is None else min(self.exit_at, self.clock[0] + timeout)
        while self.writes and self.writes[0] <= until:
            self.clock[0] = self.writes.pop(0)
            self.stream.write(b"tests/test_a.py::test_x PASSED\n")
            self.stream.flush()
        self.clock[0] = until
        if until < self.exit_at:
            raise subprocess.TimeoutExpired("cmd", timeout)
        return 0


# Every output channel counts, and the final sample keeps the last result's gap.
@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_watch_counts_every_channel_and_the_final_sample(monkeypatch, tmp_path, channel):
    clock = [0.0]
    monkeypatch.setattr(processes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(processes, "list_processes", lambda: (Row(10, 1.0, 0.0),))
    watch = SilenceWatchdog()
    with (tmp_path / "out").open("w+b") as output, (tmp_path / "err").open("w+b") as errors, \
            (tmp_path / "log").open("ab") as log:
        stream = output if channel == "stdout" else errors
        stopped = processes._watch(Process(clock, stream, [5, 300, 900, 1500], 1500), watch, output, log,
                                   lambda: None, lambda: RECORDED, channels=(errors,))
    assert stopped is False and clock[0] == 1500
    assert watch.result_gaps == (5, 295, 600, 600)


def test_watch_logs_the_decision_with_kind_and_outcome(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(processes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(processes, "list_processes", lambda: (Row(10, 1.0, 0.0),))
    with (tmp_path / "out").open("w+b") as output, (tmp_path / "log").open("ab") as log:
        assert processes._watch(Process(clock, output, [60], 5000), SilenceWatchdog(), output, log,
                                lambda: None, lambda: RECORDED)
    row = json.loads((tmp_path / "log").read_text().splitlines()[-1])
    assert (row["decision"], row["kind"], row["outcome"], row["last_output_age"]) == ("silent", "silent", "stalled-restart", 600)
    assert clock[0] == 660


# Inconsistent outcome aggregation: a pass then a teardown error is no pass.
def test_reported_passes_aggregate_every_record_of_an_id(tmp_path):
    report = tmp_path / "capture.xml"
    report.write_text('<testsuites><testsuite><testcase file="t.py" name="test_a"/>'
                      '<testcase file="t.py" name="test_a"><error message="teardown"/></testcase>'
                      '<testcase file="t.py" name="test_b"/>')
    log = tmp_path / "log"
    log.write_text("t.py::test_c PASSED [ 50%]\nt.py::test_c ERROR [ 50%]\nt.py::test_d[a b] PASSED [ 75%]\n")
    assert run_baseline_resume.reported_passes(report, log, 0) == {"t.py::test_b", "t.py::test_d[a b]"}


# Inexact selection: --deselect matches prefixes, so a kept id that prefixes another kept id reruns.
def test_resume_selection_deselects_only_prefix_safe_ids(tmp_path):
    from foragent.state import StateStore

    cache = StateStore(tmp_path / "baselines")
    cache.root.mkdir(parents=True)
    run_baseline_resume.retained_path(cache, "k").write_text(json.dumps(
        {"node_ids": ["t.py::test_work", "t.py::test_work_more", "t.py::test_other"]}))
    retained, selection = run_baseline_resume.resume_selection(cache, "k", "pytest tests")
    assert retained == {"t.py::test_work", "t.py::test_work_more", "t.py::test_other"}
    assert selection == "pytest tests --deselect=t.py::test_other --deselect=t.py::test_work_more"
    assert run_baseline_resume.deselect_count(selection) == 2


def test_a_stopped_capture_carries_its_kind_and_a_complete_one_drops_the_kept_passes(tmp_path):
    from foragent.state import StateStore

    stopped = run_checks.CheckResult("pytest", -15, True, "", stop="safety-net")
    assert run_baseline_resume.capture_failure(stopped, "x").watchdog_kind == "safety-net"
    assert not hasattr(run_baseline_resume.capture_failure(run_checks.CheckResult("pytest", 1, False, ""), "x"),
                       "watchdog_kind")
    cache = StateStore(tmp_path / "baselines")
    cache.root.mkdir(parents=True)
    log = tmp_path / "log"
    log.write_text("t.py::test_a PASSED\n")
    run_baseline_resume.after_capture(cache, "k", {"t.py::test_z"}, stopped, False, tmp_path / "none.xml", log, 0)
    assert run_baseline_resume.retained_passes(cache, "k") == {"t.py::test_a", "t.py::test_z"}
    run_baseline_resume.after_capture(cache, "k", set(), run_checks.CheckResult("pytest", 0, False, ""), True,
                                      tmp_path / "none.xml", log, 0)
    assert not run_baseline_resume.retained_path(cache, "k").exists()


# A stopped check ends the run by outcome_for: like a stall it adds to restarts; killed is a timeout.
@pytest.mark.parametrize("restarts,kind,expected", [
    (0, "silent", "stalled-restart"), (1, "safety-net", "stalled"), (0, "runtime-limit", "timeout")])
def test_end_by_watchdog_follows_outcome_for(monkeypatch, restarts, kind, expected):
    from foragent.run_attempt import ExecutorStopped

    run = SimpleNamespace(restarts=restarts)
    updates, restored = [], []
    store = SimpleNamespace(log_path=lambda n: Path("/dev/null"), get_run=lambda n: run,
                            update_run=lambda n, **fields: updates.append(fields))
    monkeypatch.setattr(run_checks, "_append", lambda *args: None)
    monkeypatch.setattr(run_waits, "_timeout", lambda n, ctx: "timeout")
    monkeypatch.setattr(scheduler, "_restore_stalled", lambda ctx, n, count, report: restored.append(count))
    with pytest.raises(ExecutorStopped) as stop:
        run_waits.end_by_watchdog(1, SimpleNamespace(store=store), kind, [], "detail")
    assert stop.value.outcome == expected
    if expected != "timeout":
        assert updates == [{"restarts": restarts + 1, "outcome": expected}] and restored == [restarts + 1]


# Dream runs are out of scope: they keep main's fixed runtime limit.
def test_dream_runs_keep_their_fixed_limit(monkeypatch):
    killed = []
    monkeypatch.setattr(scheduler, "_kill_run", lambda ctx, key, report, **kwargs: killed.append(kwargs["outcome"]))
    run = SimpleNamespace(kind="dream", key="dream-orch")
    ctx = SimpleNamespace(config=Config())
    assert scheduler_silence.check(ctx, run, SimpleNamespace(actions=[]), 100 * 60) is False
    assert scheduler_silence.check(ctx, run, SimpleNamespace(actions=[]), 100 * 60 + 1) is True
    assert killed == ["error"]


# The run's samples live in its record, written by the tick.
def test_run_samples_live_in_the_run_record(tmp_path):
    from foragent.state import StateStore

    started = datetime(2026, 10, 30, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: started)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    ctx = SimpleNamespace(store=store, config=Config(), now=lambda: started + timedelta(minutes=5))
    decision, run = scheduler_silence.observe(ctx, store.get_run(1), 300)
    assert decision.kind == "running" and run.silence["output_bytes"] == 0
    assert not (tmp_path / "state" / "silence").exists()
    assert StateStore(tmp_path / "state").get_run(1).silence == run.silence


def test_check_sites_pass_their_history_and_safety_net(tmp_path):
    cache = tmp_path / "data" / "baselines"
    cache.mkdir(parents=True)
    (cache / "a.json").write_text(json.dumps({"repository": str(tmp_path.resolve()),
                                              "created_at": "2026-10-28T00:00:00+00:00",
                                              "result_gaps_seconds": [120, 720]}))
    ctx = SimpleNamespace(store=SimpleNamespace(root=tmp_path / "data" / "owner__repo"), repo_root=tmp_path,
                          config=Config(safety_net_hours=2))
    assert watchdog.silence_args(ctx) == {"history_gaps": (120, 720), "safety_net": 7200}


from process_fakes import fake_process_table  # noqa: E402, F401


def _targeted(table, monkeypatch):
    def read(pid):
        try:
            return table.process(pid)
        except ProcessLookupError:
            return None

    monkeypatch.setattr(processes, "read_process_identity", read)
    return read


# One snapshot stop: a recorded protected process never becomes a traversal root.
@pytest.mark.parametrize("protected", ["init", "parent"])
def test_silent_stop_never_traverses_from_a_protected_process(fake_process_table, monkeypatch, tmp_path, protected):
    table = fake_process_table
    _targeted(table, monkeypatch)
    pid = 1 if protected == "init" else table.parent_pid
    unrelated = table.add(46000, ppid=pid, cwd=tmp_path, start_time=50)
    with processes.process_snapshot(), processes.quiescent_tree():
        found = processes.observe_tree([{"pid": pid, "start_time": table.rows[pid].start_time}])
    assert unrelated.pid not in {item["pid"] for item in found}


# One snapshot stop: a parent PID reused between its two reads adopts nothing.
def test_silent_stop_ignores_children_of_a_reused_parent_pid(fake_process_table, monkeypatch, tmp_path):
    table = fake_process_table
    read = _targeted(table, monkeypatch)
    root = table.add(42000, cwd=tmp_path, start_time=10)
    table.add(47000, ppid=root.pid, cwd=tmp_path, start_time=95)
    reads = []

    def reused(pid):
        reads.append(pid)
        if pid == root.pid and reads.count(pid) == 2:
            table.exit(root.pid)
            table.add(root.pid, cwd=tmp_path, start_time=90)
        return read(pid)

    with processes.process_snapshot():
        monkeypatch.setattr(processes, "read_process_identity", reused)
        with processes.quiescent_tree():
            found = processes.observe_tree([{"pid": root.pid, "start_time": 10}])
    assert 47000 not in {item["pid"] for item in found}
