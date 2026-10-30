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


# A stopped check is decided with the run's own facts: safety net > runtime limit > silent.
@pytest.mark.parametrize("stop,elapsed,kind", [
    (None, 60, "silent"), ("silent", 6001, "runtime-limit"), ("silent", 86400, "safety-net"),
    ("safety-net", 60, "safety-net")])
def test_a_stopped_check_is_decided_with_the_run_facts(stop, elapsed, kind):
    assert stopped_check_kind(stop, elapsed, Config()) == kind


def test_window_source_follows_whether_silence_minutes_is_set():
    assert watchdog.silence_window(Config()) == (600, "default")
    assert watchdog.silence_window(Config(silence_minutes=10)) == (600, "config")
    assert SilenceWatchdog(window_seconds=600).window_source == "config"
    assert SilenceWatchdog().window_source == "default"


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
    passed, failed = run_baseline_resume.reported_results(report, log, 0, tmp_path / "none.jsonl", tmp_path)
    assert passed == {"t.py::test_b", "t.py::test_d[a b]"}
    assert failed == {"t.py::test_a", "t.py::test_c"}


# Inexact selection: --deselect matches prefixes, so a kept id that prefixes another kept id reruns.
def test_resume_selection_deselects_only_prefix_safe_ids(tmp_path):
    from foragent.state import StateStore

    cache = StateStore(tmp_path / "baselines")
    cache.root.mkdir(parents=True)
    run_baseline_resume.retained_path(cache, "k").write_text(json.dumps(
        {"node_ids": ["t.py::test_work", "t.py::test_work_more", "t.py::test_other"]}))
    work = tmp_path / "work"
    work.mkdir()
    retained, selection, _ = run_baseline_resume.prepare(cache, "k", "pytest tests", work, tmp_path)
    assert retained == {"t.py::test_work", "t.py::test_work_more", "t.py::test_other"}
    assert "pytest tests --deselect=t.py::test_other --deselect=t.py::test_work_more -p _foragent_results_" in selection
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
    run_baseline_resume.retained_path(cache, "k").write_text(json.dumps({"node_ids": ["t.py::test_z"]}))
    run_baseline_resume.after_capture(cache, "k", stopped, False, tmp_path / "none.xml", log, 0,
                                      tmp_path / "none.jsonl", tmp_path)
    assert run_baseline_resume.retained_passes(cache, "k") == {"t.py::test_a", "t.py::test_z"}
    run_baseline_resume.after_capture(cache, "k", run_checks.CheckResult("pytest", 0, False, ""), True,
                                      tmp_path / "none.xml", log, 0, tmp_path / "none.jsonl", tmp_path)
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


# A decision voided by progress written after it was taken: _kill_run must see the change.
def test_the_decided_record_is_the_one_the_decision_read(tmp_path):
    from foragent.state import StateStore

    started = datetime(2026, 10, 30, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: started)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    before = store.get_run(1)
    write = store.update_run

    def runner_progresses(key, **fields):
        if "silence" in fields:  # The runner records progress between the observation and this write.
            write(key, last_progress_at=(started + timedelta(minutes=30)).isoformat())
        return write(key, **fields)

    store.update_run = runner_progresses
    ctx = SimpleNamespace(store=store, config=Config(), now=lambda: started + timedelta(minutes=30))
    decision, decided = scheduler_silence.observe(ctx, before, 1800)
    assert decision.kind == "silent"
    assert decided.last_progress_at == before.last_progress_at != store.get_run(1).last_progress_at


def _pytest_project(tmp_path, source):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "tests" / "test_a.py").write_text(source)
    return project


# Inexact selection: --deselect drops prefixes; only the exactly kept id stays out, even without a terminal.
def test_resumed_capture_runs_every_test_but_the_exactly_kept_ones(tmp_path):
    import sys

    from foragent.state import StateStore

    project = _pytest_project(tmp_path, "def test_a(): pass\ndef test_ab(): pass\n")
    cache = StateStore(tmp_path / "baselines")
    cache.root.mkdir(parents=True)
    run_baseline_resume.retained_path(cache, "k").write_text(json.dumps({"node_ids": ["tests/test_a.py::test_a"]}))
    report = tmp_path / "out.xml"
    work = tmp_path / "work"
    work.mkdir()
    command = (f"{sys.executable} -m pytest -p no:cacheprovider -p no:terminal tests "
               f"-o junit_family=legacy --junit-prefix= --junitxml={report}")
    retained, command, listing = run_baseline_resume.prepare(cache, "k", command, work, project)
    assert "--deselect=tests/test_a.py::test_a" in command
    subprocess.run(["/bin/sh", "-c", command], cwd=project, capture_output=True, check=False)
    assert run_checks._passing_acceptance_ids(report) == ("tests/test_a.py::test_ab",)
    assert run_baseline_resume.selected(2, "collected 2 items / 2 deselected", listing, command) == 1


# Lost passes: at -q pytest prints dots and writes JUnit at the end, so the plugin records each pass.
def test_a_stopped_capture_keeps_the_passes_the_plugin_recorded(tmp_path):
    import sys

    from foragent.state import StateStore

    project = _pytest_project(tmp_path, "import pytest\n"
                              "def test_pass(): pass\n"
                              "def test_fail(): assert False\n"
                              "@pytest.fixture\n"
                              "def broken():\n    yield\n    raise RuntimeError('teardown')\n"
                              "def test_teardown(broken): pass\n")
    cache = StateStore(tmp_path / "baselines")
    cache.root.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    retained, command, listing = run_baseline_resume.prepare(
        cache, "k", f"{sys.executable} -m pytest -q -p no:cacheprovider tests", work, project)
    subprocess.run(["/bin/sh", "-c", command], cwd=project, capture_output=True, check=False)
    stopped = run_checks.CheckResult(command, -15, True, "", stop="silent")
    log = tmp_path / "log"
    log.write_text("")
    run_baseline_resume.after_capture(cache, "k", stopped, False, tmp_path / "none.xml", log, 0, listing, project)
    assert run_baseline_resume.retained_passes(cache, "k") == {"tests/test_a.py::test_pass"}


# A setting lost at a process boundary: the detached builder keeps the configured safety net.
def test_detached_baseline_builder_keeps_the_configured_safety_net(monkeypatch, tmp_path):
    from foragent import run_baseline, run_baseline_process

    seen = []
    monkeypatch.setattr(run_baseline, "_build_baseline", lambda context, build, lock: seen.append(context.config))
    monkeypatch.setattr(run_baseline_process, "load_record", lambda cls, value: value)
    monkeypatch.setattr(run_baseline_process.os, "fdopen", lambda fd, mode: None)
    requests = []
    monkeypatch.setattr(run_baseline.subprocess, "Popen", lambda argv, **kwargs: requests.append(json.loads(argv[3])))
    ctx = SimpleNamespace(repo_root=tmp_path, store=SimpleNamespace(root=tmp_path / "state"),
                          config=Config(safety_net_hours=2))
    (tmp_path / "baselines").mkdir()
    lock = (tmp_path / "lock").open("a")
    build = run_baseline.BaselineBuild(base_sha="s", test_command="pytest", excluded=[], key="k", scope="x")
    run_baseline.start_baseline_process(ctx, build, lock)
    run_baseline_process.main(["x", json.dumps({**requests[0], "build": {}}), "9"])
    assert seen[0].safety_net_hours == 2


# Selection and count agree: every exactly kept id stays out, also one that prefixes another kept id.
def test_resumed_capture_leaves_out_every_kept_id_and_counts_what_it_left_out(tmp_path):
    import sys

    from foragent.state import StateStore

    project = _pytest_project(tmp_path, "def test_a(): pass\ndef test_ab(): pass\ndef test_b(): pass\n")
    cache = StateStore(tmp_path / "baselines")
    cache.root.mkdir(parents=True)
    run_baseline_resume.retained_path(cache, "k").write_text(json.dumps(
        {"node_ids": ["tests/test_a.py::test_a", "tests/test_a.py::test_ab"]}))
    report, work = tmp_path / "out.xml", tmp_path / "work"
    work.mkdir()
    command = (f"{sys.executable} -m pytest -p no:cacheprovider tests "
               f"-o junit_family=legacy --junit-prefix= --junitxml={report}")
    retained, command, listing = run_baseline_resume.prepare(cache, "k", command, work, project)
    assert "--deselect=tests/test_a.py::test_a " not in command + " "  # A prefix of another kept id.
    output = subprocess.run(["/bin/sh", "-c", command], cwd=project, capture_output=True, text=True).stdout
    assert run_checks._passing_acceptance_ids(report) == ("tests/test_a.py::test_b",)
    line = next(line for line in output.splitlines() if line.startswith("collected"))
    assert run_baseline_resume.selected(3, line, listing, command) == 1  # Equal to what JUnit recorded.


def test_a_kept_id_run_again_keeps_its_fresh_result(tmp_path):
    log = tmp_path / "log"
    log.write_text("")
    passing, collected, failing = run_baseline_resume.combine(
        ("t.py::test_b",), ("t.py::test_a", "t.py::test_b"), ("t.py::test_a",), {"t.py::test_a", "t.py::test_z"},
        log, 0, tmp_path)
    assert passing == ("t.py::test_b", "t.py::test_z")  # test_a failed when it ran again.
    assert collected == ("t.py::test_a", "t.py::test_b", "t.py::test_z") and failing == ("t.py::test_a",)


# Inconsistent identity: one test_id, realpath on the roots only, relative to the worktree root.
def test_one_identity_matches_symlinked_roots_and_keeps_outside_ids_raw(tmp_path):
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert run_baseline_resume.test_id("t.py::a", real, link) == "t.py::a"  # /private/var vs /var
    assert run_baseline_resume.test_id("t.py::a", real / "sub", link) == "sub/t.py::a"
    assert run_baseline_resume.test_id("t.py::a", tmp_path / "elsewhere", real) == "t.py::a"
    # The generated plugin carries the same function, not a second implementation.
    import inspect
    assert inspect.getsource(run_baseline_resume.test_id) in run_baseline_resume.RESULTS_PLUGIN.format(
        listing="x", keep=[], repo="r", test_id=inspect.getsource(run_baseline_resume.test_id))


def test_resumed_capture_through_a_symlinked_worktree_does_not_rerun_kept_tests(tmp_path):
    import sys

    from foragent.state import StateStore

    real = _pytest_project(tmp_path, "def test_a(): pass\ndef test_b(): pass\n")
    link = tmp_path / "linked"
    link.symlink_to(real)
    cache = StateStore(tmp_path / "baselines")
    cache.root.mkdir(parents=True)
    run_baseline_resume.retained_path(cache, "k").write_text(json.dumps({"node_ids": ["tests/test_a.py::test_a"]}))
    report, work = tmp_path / "out.xml", tmp_path / "work"
    work.mkdir()
    command = (f"{sys.executable} -m pytest -p no:cacheprovider --rootdir={real} tests "
               f"-o junit_family=legacy --junit-prefix= --junitxml={report}")
    retained, command, listing = run_baseline_resume.prepare(cache, "k", command, work, link)
    subprocess.run(["/bin/sh", "-c", command], cwd=link, capture_output=True, check=False)
    assert run_checks._passing_acceptance_ids(report) == ("tests/test_a.py::test_b",)


# Premature promotion: any non-pass from any source wins, now and in later captures.
def test_a_failure_stays_saved_until_a_normal_end_capture(tmp_path):
    from foragent.state import StateStore

    cache = StateStore(tmp_path / "baselines")
    cache.root.mkdir(parents=True)
    log = tmp_path / "log"
    stopped = run_checks.CheckResult("pytest", -15, True, "", stop="silent")
    ended = run_checks.CheckResult("pytest", 1, False, "")

    def capture(result, lines, complete=False):
        log.write_text(lines)
        run_baseline_resume.after_capture(cache, "k", result, complete, tmp_path / "none.xml", log, 0,
                                          tmp_path / "none.jsonl", tmp_path)

    capture(stopped, "t.py::test_x PASSED\nt.py::test_y PASSED\n")
    capture(ended, "t.py::test_x FAILED\nt.py::test_z PASSED\n")  # A later failure removes it; ended adds no pass.
    assert run_baseline_resume.retained_passes(cache, "k") == {"t.py::test_y"}
    capture(stopped, "t.py::test_x PASSED\n")  # A later PASSED cannot bring it back.
    assert run_baseline_resume.retained_passes(cache, "k") == {"t.py::test_y"}
    capture(ended, "", complete=True)  # Only a normal-end capture clears the record.
    assert not run_baseline_resume.retained_path(cache, "k").exists()


def test_plugin_records_every_non_pass_and_only_full_passes(tmp_path):
    import sys

    from foragent.state import StateStore

    project = _pytest_project(tmp_path, "import pytest\n"
                              "def test_pass(): pass\n"
                              "def test_fail(): assert False\n"
                              "@pytest.mark.skip\n"
                              "def test_skip(): pass\n"
                              "@pytest.mark.xfail\n"
                              "def test_xfail(): assert False\n"
                              "@pytest.mark.xfail\n"
                              "def test_xpass(): pass\n"
                              "@pytest.fixture\n"
                              "def broken():\n    yield\n    raise RuntimeError('teardown')\n"
                              "def test_teardown(broken): pass\n")
    cache = StateStore(tmp_path / "baselines")
    cache.root.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    _, command, listing = run_baseline_resume.prepare(cache, "k", f"{sys.executable} -m pytest -q -p no:cacheprovider tests",
                                                      work, project)
    subprocess.run(["/bin/sh", "-c", command], cwd=project, capture_output=True, check=False)
    passed, failed = run_baseline_resume.reported_results(tmp_path / "none.xml", tmp_path / "none.log", 0, listing, project)
    assert passed == {"tests/test_a.py::test_pass"}
    assert failed == {f"tests/test_a.py::test_{name}" for name in ("fail", "skip", "xfail", "xpass", "teardown")}


def test_cache_key_carries_the_identity_version(tmp_path):
    from foragent import run_baseline

    ctx = SimpleNamespace(config=Config(), repo_root=tmp_path)
    assert run_baseline._baseline_identity(ctx, "pytest", "sha")[-1] == run_baseline_resume.CACHE_VERSION


# Incomplete stop propagation: a watchdog-stopped related check ends the run for every caller
# (implementation and CI review), with the checks gathered before it.
def test_a_stopped_related_check_ends_the_run_for_every_caller(monkeypatch, tmp_path):
    from foragent import runner
    from foragent.run_attempt import ExecutorStopped
    from foragent.state import StateStore

    started = datetime(2026, 10, 30, tzinfo=UTC)
    store = StateStore(tmp_path / "state", now=lambda: started)
    store.start_run(1, branch="b", worktree=tmp_path / "w")
    ctx = SimpleNamespace(store=store, config=Config(), now=lambda: started + timedelta(minutes=5),
                          repo_root=tmp_path, monotonic=lambda: 0.0)
    monkeypatch.setattr(runner, "_check", lambda command, *a, **k: run_checks.CheckResult(command, -15, True, "", stop="silent"))
    monkeypatch.setattr(run_checks, "_test_slot", lambda n, ctx: None)
    ended = []

    def end(n, ctx, kind, checks, detail):
        ended.append((kind, [check.command for check in checks]))
        raise ExecutorStopped("stalled-restart", detail)

    monkeypatch.setattr(run_waits, "end_by_watchdog", end)
    earlier = run_checks.CheckResult("done-when", 0, False, "")
    with pytest.raises(ExecutorStopped):
        run_checks.run_related_check(1, ctx, tmp_path, "pytest", ["tests/test_a.py"], 1, 900, gathered=[earlier])
    assert ended == [("silent", ["done-when", ended[0][1][1]])] and "tests/test_a.py" in ended[0][1][1]
    with pytest.raises(ExecutorStopped):  # CI review passes nothing gathered: the check alone.
        run_checks.run_related_check(1, ctx, tmp_path, "pytest", ["tests/test_a.py"], 1, 900)
    assert len(ended[1][1]) == 1
