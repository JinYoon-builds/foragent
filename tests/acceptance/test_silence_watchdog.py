"""Acceptance test for silence-watchdog. Locked by Foragent; do not edit."""

from dataclasses import dataclass, replace
from datetime import timedelta
import importlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from conftest import BASE_TIME, git_command
from foragent import processes, run_baseline, run_checks, run_waits, runner, scheduler
from foragent.config import Config
from foragent.run_attempt import AttemptFailure, ExecutorStopped
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from test_orch_review_handoff import make_case
from test_stop_whole_tree import (
    FakeProcess, FakeProcessTable, _ModuleView, fake_processes, record_tree, stop_context,
)


NODES = tuple(f"tests/acceptance/test_work.py::test_part_{i}" for i in range(8))
DEFAULT_WINDOW = 600
SAFETY_NET = 24 * 60 * 60


@dataclass(frozen=True)
class CpuProcess(FakeProcess):
    # The immutable #431 snapshot carries cumulative user + system CPU seconds.
    cpu_time: float = 0.0


def cpu(table, pid, seconds):
    row = table.rows[pid]
    values = {**vars(row), "cpu_time": float(seconds)}
    table.rows[pid] = CpuProcess(**values)


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.listeners = []

    def monotonic(self):
        return self.seconds

    def now(self):
        return BASE_TIME + timedelta(seconds=self.seconds)

    def sleep(self, seconds):
        assert seconds >= 0
        self.seconds += seconds
        for listener in tuple(self.listeners):
            listener()


class DormantThread:
    """No real observer, heartbeat or helper thread; fake waits advance the clock."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


@pytest.fixture(autouse=True)
def guarded_io(fake_processes, monkeypatch, tmp_path):
    """Reuse #261's fail-closed signal/fork/helper guards, including signal zero."""
    assert isinstance(fake_processes, FakeProcessTable)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    def identity(pid):
        try:
            return fake_processes.process(pid)
        except ProcessLookupError:
            return None

    monkeypatch.setattr(processes, "read_process_identity", identity)
    for module in (processes, run_waits):
        monkeypatch.setattr(module, "threading", _ModuleView(threading, Thread=DormantThread))
    yield
    assert fake_processes.host_pids <= fake_processes.live, "A protected host process was touched"
    assert not fake_processes.leaderless_group_calls, "Numeric leaderless groups must never be signaled"


def watchdog(**kwargs):
    """Public seam chosen here: SilenceWatchdog(history_gaps=(), safety_net=86400).

    observe(now, output_bytes, progress_events, snapshot, identities) returns facts
    with stop, decision, last_output_age, cpu_delta, window_seconds, window_source.
    Counters are cumulative and snapshot is supplied, never enumerated here.
    """
    try:
        module = importlib.import_module("foragent.watchdog")
    except ModuleNotFoundError as exc:
        if exc.name != "foragent.watchdog":
            raise
        pytest.fail("Missing shared foragent.watchdog: all three sites need the same silence decision")
    return module.SilenceWatchdog(**kwargs)


def observe(watch, table, clock, identities, *, output=0, progress=0):
    return watch.observe(
        now=clock.monotonic(), output_bytes=output, progress_events=progress,
        snapshot=table.list_processes(), identities=identities,
    )


def facts(case):
    """Decision facts are structured log records, either in the run log or events."""
    records = []
    paths = [case.store.log_path(1), case.store.root / "orch-events.jsonl"]
    paths.extend((case.store.root.parent / "baselines").glob("*.log"))
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and "decision" in value:
                records.append(value)
    return records


def assert_facts(case, decision, *, age=None, delta=None, window=DEFAULT_WINDOW, source="default"):
    rows = [row for row in facts(case) if row["decision"] == decision]
    assert rows, f"No logged watchdog decision={decision!r} with its observations"
    row = rows[-1]
    assert {"last_output_age", "cpu_delta", "window_seconds", "window_source"} <= row.keys(), row
    assert row["window_seconds"] == window, row
    assert row["window_source"] == source, row
    if age is not None:
        assert row["last_output_age"] == pytest.approx(age), row
    if delta is not None:
        assert row["cpu_delta"] == pytest.approx(delta), row
    return row


@pytest.mark.parametrize("activity", ["output", "cpu", "progress"])
def test_shared_watchdog_requires_both_output_and_tree_cpu_silence(fake_processes, tmp_path, activity):
    table, clock = fake_processes, Clock()
    root = table.add(52000, cwd=tmp_path, start_time=10)
    child = table.add(52001, ppid=root.pid, cwd=tmp_path, start_time=11)
    for row in (root, child):
        cpu(table, row.pid, 0)
    identities = [{"pid": row.pid, "start_time": row.start_time} for row in (root, child)]
    watch = watchdog()
    output = progress = 0
    assert not observe(watch, table, clock, identities).stop
    for _ in range(8):
        clock.sleep(300)
        output += int(activity == "output")
        progress += int(activity == "progress")
        if activity == "cpu":
            cpu(table, child.pid, clock.seconds / 10)
        decision = observe(watch, table, clock, identities, output=output, progress=progress)
        assert not decision.stop, f"40-minute work was stopped despite {activity} growth"
    clock.sleep(DEFAULT_WINDOW - 1)
    assert not observe(watch, table, clock, identities, output=output, progress=progress).stop
    clock.sleep(1)
    decision = observe(watch, table, clock, identities, output=output, progress=progress)
    assert decision.stop and decision.decision == "silent"
    assert decision.cpu_delta == 0
    assert decision.window_seconds == DEFAULT_WINDOW
    assert decision.window_source == "default"
    assert decision.last_output_age == (3000 if activity == "cpu" else DEFAULT_WINDOW)
    assert not table.signals, "The shared decision function itself must never signal"


def test_shared_watchdog_measures_window_from_longest_result_gap(fake_processes):
    clock = Clock()
    watch = watchdog(history_gaps=(120, 720, 480))
    first = observe(watch, fake_processes, clock, [])
    assert first.window_seconds == 1440, "Use twice the longest result gap, not total command duration"
    assert first.window_source == "history"
    clock.sleep(1439)
    assert not observe(watch, fake_processes, clock, []).stop
    clock.sleep(1)
    assert observe(watch, fake_processes, clock, []).stop


def test_shared_watchdog_tracks_detached_cpu_and_rejects_reused_pid(fake_processes, tmp_path):
    table, clock = fake_processes, Clock()
    root = table.add(52000, cwd=tmp_path, start_time=10)
    detached = table.add(52001, ppid=root.pid, cwd=tmp_path, start_time=11)
    for row in (root, detached):
        cpu(table, row.pid, 0)
    identities = [{"pid": row.pid, "start_time": row.start_time} for row in (root, detached)]
    watch = watchdog()
    observe(watch, table, clock, identities)
    # Keep the recorded child after it leaves PPID, process group, session and cwd.
    table.rows[detached.pid] = replace(table.rows[detached.pid], ppid=1, cwd=tmp_path.parent)
    clock.sleep(600)
    cpu(table, detached.pid, 100)
    assert not observe(watch, table, clock, identities).stop, "A recorded detached child's CPU is progress"
    table.exit(detached.pid)
    table.add(detached.pid, cwd=tmp_path, start_time=999)
    cpu(table, detached.pid, 10000)
    clock.sleep(600)
    assert observe(watch, table, clock, identities).stop, "A reused PID's CPU must not keep old work alive"
    assert not table.signals


def test_shared_watchdog_safety_net_can_end_work_that_is_still_progressing(fake_processes):
    clock, watch = Clock(), watchdog()
    observe(watch, fake_processes, clock, [])
    for _ in range(SAFETY_NET // 300 - 1):
        clock.sleep(300)
        assert not observe(watch, fake_processes, clock, [], output=int(clock.seconds)).stop
    clock.sleep(300)
    decision = observe(watch, fake_processes, clock, [], output=int(clock.seconds))
    assert decision.stop and decision.decision == "safety-net"
    assert decision.last_output_age == 0


class Command:
    """Popen file I/O plus a virtual process lifetime; no watchdog policy lives here.

    wait(timeout) obeys Popen semantics. Production chooses how often to sample,
    how to interpret output/CPU, and when to stop the recorded tree.
    """

    def __init__(self, owner, argv, *, cwd, stdout, **kwargs):
        self.owner, self.args, self.stdout = owner, argv, stdout
        self.started = owner.clock.seconds
        self.returncode = None
        self.pid = 52000 + 10 * len(owner.commands)
        self.duration, self.activity = owner.duration, owner.activity
        self.report_mode = owner.report_mode
        words = shlex.split(argv[-1])
        selectors = {word for word in words if "::test_part_" in word and not word.startswith("-")}
        excluded = {word.split("=", 1)[1] for word in words if word.startswith("--deselect=")}
        excluded.update(words[i + 1] for i, word in enumerate(words[:-1]) if word == "--deselect")
        self.nodes = tuple(node for node in owner.nodes if (not selectors or node in selectors) and node not in excluded)
        self.report = next((Path(word.split("=", 1)[1]) for word in words if word.startswith("--junitxml=")), None)
        self.emitted = []
        self.tree = []
        for offset in range(4):
            parent = 1 if offset == 0 else self.pid + offset - 1
            row = owner.table.add(self.pid + offset, ppid=parent, cwd=cwd,
                                  start_time=owner.clock.now().timestamp() + offset / 100)
            cpu(owner.table, row.pid, 0)
            self.tree.append(row)
        owner.commands.append(self)

    def _report(self, complete):
        if self.report is None or (not complete and self.report_mode == "streamed"):
            return
        cases = "".join(ET.tostring(ET.Element(
            "testcase", file=node.split("::")[0], name=node.split("::")[1],
            time="300",
        ), encoding="unicode") for node in self.emitted)
        self.report.write_text("<testsuites><testsuite>" + cases
                               + ("</testsuite></testsuites>" if complete else ""), encoding="utf-8")

    def pump(self):
        if self.returncode is not None or self.pid not in self.owner.table.live:
            return
        # The launch snapshot has witnessed this child before it reparents.
        detached = self.owner.table.rows[self.tree[-1].pid]
        self.owner.table.rows[detached.pid] = replace(detached, ppid=1, cwd=self.owner.root.parent)
        elapsed = min(self.owner.clock.seconds - self.started, self.duration)
        if self.activity in {"both", "cpu"}:
            # Only the detached grandchild consumes CPU; the shell stays flat.
            cpu(self.owner.table, self.tree[-1].pid, elapsed)
        for index, node in enumerate(self.nodes):
            at = 60 if self.activity == "then-silent" and index == 0 else 300 * (index + 1)
            if self.activity == "then-silent" and index > 0:
                continue
            if elapsed >= at and node not in self.emitted:
                self.emitted.append(node)
                if self.activity in {"both", "output", "then-silent"}:
                    self.stdout.write(f"{node} PASSED\n".encode())
                    self.stdout.flush()
                self._report(complete=False)
        if elapsed >= self.duration:
            self.emitted = list(self.nodes)
            self._report(complete=True)
            self.returncode = 0
            for row in reversed(self.tree):
                self.owner.table.exit(row.pid)

    def poll(self):
        self.pump()
        if self.returncode is None and self.pid not in self.owner.table.live:
            self.returncode = -15
        return self.returncode

    def wait(self, timeout=None):
        if self.poll() is not None:
            return self.returncode
        remaining = self.started + self.duration - self.owner.clock.seconds
        self.owner.clock.sleep(remaining if timeout is None else min(remaining, timeout))
        if self.poll() is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode


@pytest.fixture
def command_case(tmp_path, monkeypatch, fake_processes):
    clock = Clock()
    root = tmp_path / "repo"
    root.mkdir()
    store = StateStore(tmp_path / "state", now=clock.now)
    store.start_run(1, branch="foragent/issue-1", worktree=root)
    case = SimpleNamespace(clock=clock, root=root, store=store, table=fake_processes,
                           duration=2400, activity="both", report_mode="partial-junit",
                           nodes=NODES, commands=[])

    def add_worktree(path, sha):
        test = path / NODES[0].split("::")[0]
        test.parent.mkdir(parents=True)
        test.write_text("\n".join(f"def test_part_{i}(): pass" for i in range(8)))

    git = SimpleNamespace(ref_sha=lambda ref: "base", blob_sha=lambda *args: None,
                          add_detached_worktree=add_worktree, remove_worktree=lambda root: None)
    tracker = FakeTracker(now=clock.now)
    tracker.add_issue(1, "Work", labels=["approved", "running"])
    case.ctx = runner.RunContext(
        "owner/repo", root, tracker, store,
        Config(test_command="pytest", max_runtime_minutes=90), git,
        now=clock.now, monotonic=clock.monotonic, sleep=clock.sleep,
    )
    clock.listeners.append(lambda: [command.pump() for command in tuple(case.commands)])
    monkeypatch.setattr(processes, "time", _ModuleView(time, monotonic=clock.monotonic, sleep=clock.sleep))
    monkeypatch.setattr(run_baseline, "monotonic", clock.monotonic)
    guarded_popen = subprocess.Popen

    def popen(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
            return guarded_popen(argv, *args, **kwargs)
        assert not args and argv[:2] == ["/bin/sh", "-c"], f"Unexpected helper: {argv!r}"
        return Command(case, argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    case.capture = lambda: run_baseline._capture_acceptance_baseline(1, case.ctx, "pytest", "main")
    return case


def capture(case, site):
    if site == "baseline":
        return case.capture()
    build = run_baseline._baseline_build(case.ctx, "pytest", "main", case.store.log_path(1))
    cache = StateStore(case.store.root.parent / "baselines", now=case.clock.now)
    run_baseline._build_baseline(case.ctx, build, cache.lock_acceptance_baseline(build.key))
    saved = cache.load_acceptance_baseline(build.key)
    return None if saved is None else run_baseline._saved_baseline("pytest", saved)


def verify(case, site):
    (case.root / "tests").mkdir(exist_ok=True)
    (case.root / "tests/test_work.py").write_text("def test_work(): pass\n")
    # Git facts only; verification and its timeout/selection decisions are real.
    git = SimpleNamespace(stage_changes=lambda: None, staged_tree_sha=lambda: "unchanged",
                          changed_paths=lambda ref: ["tests/test_work.py"],
                          check_publication=lambda ref: None)
    attempt = SimpleNamespace(
        n=1, ctx=case.ctx, git=git, worktree=case.root, log=case.store.log_path(1),
        parsed=SimpleNamespace(done_when_commands=["pytest tests/test_work.py"] if site == "done-when" else []),
        batch_branch=None, start_ref="main", target_branch="main", baseline=None,
    )
    if site == "done-when":
        git.changed_paths = lambda ref: ["feature.txt"]
    return run_checks._verify_implementation(attempt)


@pytest.mark.parametrize("activity", ["both", "output", "cpu"])
@pytest.mark.parametrize("site", ["baseline", "baseline-prebuild", "done-when", "related"])
def test_each_command_site_finishes_forty_minutes_of_progress_without_restarting(command_case, site, activity):
    case = command_case
    case.activity = activity
    if site in {"baseline", "baseline-prebuild"}:
        try:
            baseline = capture(case, site)
        except AttemptFailure as exc:
            pytest.fail(f"Baseline ended while {activity} was growing: {exc}")
        assert baseline is not None, f"Prebuild ended while {activity} was growing"
        assert baseline.node_ids == NODES
    else:
        if site == "related":
            # The old related-test budget has a 900s floor, even for a 2min run.
            case.ctx.config = replace(case.ctx.config, max_runtime_minutes=2)
        try:
            failure = verify(case, site)
        except ExecutorStopped as exc:
            pytest.fail(f"{site} ended while {activity} was growing: {exc.outcome}: {exc}")
        assert not failure, f"{site} ended while {activity} was growing: {failure}"
    assert case.clock.seconds == 2400
    assert len(case.commands) == 1, "Progressing work must finish in its original invocation"
    assert not case.table.signals, "A progressing command received a stop signal"


@pytest.mark.parametrize("site", ["baseline", "baseline-prebuild", "done-when", "related"])
def test_each_command_site_uses_recorded_result_gaps_for_its_silence_window(command_case, site):
    case = command_case
    # Extend the existing durable capture history with measured result gaps.
    # Deliberately distinguish 2*max(gaps)=1440 from 2*total duration=2640.
    cache = case.store.root.parent / "baselines"
    cache.mkdir(exist_ok=True)
    (cache / "previous-base.json").write_text(json.dumps({
        "node_ids": NODES[:3], "collected_ids": NODES[:3], "failing_ids": [], "excluded": [],
        "repository": str(case.root.resolve()), "command": "pytest",
        "created_at": (case.clock.now() - timedelta(days=1)).isoformat(),
        "duration_seconds": 1320, "result_gaps_seconds": [120, 720, 480],
    }))
    case.duration, case.activity = 7200, "then-silent"
    try:
        if site in {"baseline", "baseline-prebuild"}:
            capture(case, site)
        else:
            verify(case, site)
    except (AttemptFailure, ExecutorStopped):
        pass
    assert len(case.commands) == 1, "History must adjust the live command, not retry it"
    assert 1500 <= case.clock.seconds <= 1505, "Use the measured 1440s silence window after the t=60 result"
    assert_facts(case, "silent", age=1440, delta=0, window=1440, source="history")


def test_check_stops_after_actual_silence_and_logs_both_facts(command_case):
    case = command_case
    case.duration, case.activity = 3600, "then-silent"
    # Harness log writes are not output from the monitored command.
    case.clock.listeners.append(lambda: run_checks._append(case.store.log_path(1), "scheduler heartbeat"))
    result = run_checks._check("pytest tests/acceptance", case.root, case.store.log_path(1), 900,
                              progress=lambda: run_waits._progress(1, case.ctx))
    assert result.timed_out and result.exit_code != 0
    assert 660 <= case.clock.seconds <= 665, "Silence starts at the last result (t=60), not launch or 900s"
    assert {row.pid for row in case.commands[0].tree}.isdisjoint(case.table.live)
    assert_facts(case, "silent", age=DEFAULT_WINDOW, delta=0)


@pytest.mark.parametrize("report_mode", ["partial-junit", "streamed"])
def test_silent_baseline_retains_passes_and_next_capture_runs_only_remaining_ids(command_case, report_mode):
    case = command_case
    case.nodes = NODES[:2]
    case.duration, case.activity, case.report_mode = 3600, "then-silent", report_mode
    try:
        case.capture()
    except AttemptFailure:
        pass  # An incomplete baseline must still refuse to verify a merge.
    else:
        pytest.fail("A silent partial capture was accepted as a complete baseline")
    cache, key = run_baseline._baseline_cache(case.ctx, "pytest", "base", [])
    assert cache.load_acceptance_baseline(key) is None, "Partial passes must not become a complete baseline"
    # Reload persisted state: the next invocation cannot rely on in-memory ids.
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.clock.now))
    case.duration, case.activity = 300, "both"
    baseline = case.capture()
    assert case.commands[0].emitted == [NODES[0]], "The fixture must report exactly one passing result"
    assert case.commands[1].nodes == (NODES[1],), "The next capture restarted a test already reported passing"
    assert baseline.node_ids == NODES[:2], "Completion must combine retained passes with resumed results"
    assert tuple(cache.load_acceptance_baseline(key)["node_ids"]) == NODES[:2]


def runtime_case(git_history, tmp_path, monkeypatch, table, **config):
    case = make_case(git_history, tmp_path, monkeypatch, **{
        "executor": "codex", "reviewer": "none", "skill_libraries": [],
        "test_command": "true", "max_runtime_minutes": 90, "stall_minutes": 120, **config,
    })
    tree = record_tree(case, table)
    for row in tree:
        cpu(table, row.pid, 0)
    # The last child is recorded before leaving all current-parent evidence.
    table.rows[tree[-1].pid] = replace(table.rows[tree[-1].pid], ppid=1)
    return case, tree


def runtime_tick(case, table):
    table.list_calls = 0
    return scheduler.tick(stop_context(case, table))


@pytest.mark.parametrize("activity", ["output", "cpu", "progress"])
def test_run_runtime_survives_old_limit_while_any_progress_fact_grows(
    git_history, tmp_path, monkeypatch, fake_processes, activity,
):
    case, tree = runtime_case(git_history, tmp_path, monkeypatch, fake_processes)
    runtime_tick(case, fake_processes)
    for step in range(1, 25):
        case.clock[0] += timedelta(minutes=5)
        if activity == "output":
            run_checks._append(case.store.log_path(1), f"Result {step} PASSED")
        elif activity == "cpu":
            cpu(fake_processes, tree[-1].pid, step * 30)
        else:
            case.store.update_run(1, last_progress_at=case.clock[0].isoformat())
        report = runtime_tick(case, fake_processes)
        assert case.store.get_run(1).status == "running", (
            f"Run ended at {step * 5} minutes despite {activity} growth: {report.actions}"
        )
        assert not fake_processes.signals
    assert fake_processes.list_calls == 1, "Watchdog CPU must come from the tick's single #431 snapshot"


def test_run_ends_at_default_silence_window_instead_of_configured_runtime(
    git_history, tmp_path, monkeypatch, fake_processes,
):
    case, tree = runtime_case(git_history, tmp_path, monkeypatch, fake_processes)
    runtime_tick(case, fake_processes)
    case.clock[0] += timedelta(seconds=599)
    runtime_tick(case, fake_processes)
    assert case.store.get_run(1).status == "running", "Stop before the silence window elapsed"
    assert not fake_processes.signals
    case.clock[0] += timedelta(seconds=1)
    runtime_tick(case, fake_processes)
    assert case.store.get_run(1).status == "finished", "Flat CPU and no output must stop at 600s, not 100 minutes"
    assert {row.pid for row in tree}.isdisjoint(fake_processes.live), "Silent run left a recorded descendant alive"
    assert_facts(case, "silent", age=600, delta=0)
    assert fake_processes.list_calls == 1, "Termination must reuse the tick snapshot, with targeted identity rechecks"


def test_run_stop_logs_decision_and_observed_facts(git_history, tmp_path, monkeypatch, fake_processes):
    case, _ = runtime_case(git_history, tmp_path, monkeypatch, fake_processes)
    runtime_tick(case, fake_processes)
    case.clock[0] += timedelta(minutes=101)
    runtime_tick(case, fake_processes)
    assert case.store.get_run(1).status == "finished"
    assert_facts(case, "silent", age=6060, delta=0)


def test_all_three_sites_consult_the_shared_public_watchdog(
    command_case, git_history, tmp_path, monkeypatch, fake_processes,
):
    watch_type = type(watchdog())
    original = watch_type.observe
    active, seen = [None], set()

    def traced(self, *args, **kwargs):
        seen.add(active[0])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(watch_type, "observe", traced)
    case = command_case
    case.duration, case.nodes = 60, NODES[:1]
    # Different base identities prevent a warm cache from skipping observation.
    case.ctx.git.ref_sha = lambda ref: active[0]
    for site in ("baseline", "baseline-prebuild", "done-when", "related"):
        active[0] = site
        if site in {"baseline", "baseline-prebuild"}:
            assert capture(case, site) is not None
        else:
            assert not verify(case, site)
        assert site in seen, f"{site} did not consult the shared watchdog"
    active[0] = "runtime"
    runtime, _ = runtime_case(git_history, tmp_path, monkeypatch, fake_processes)
    runtime_tick(runtime, fake_processes)
    assert "runtime" in seen, "Run runtime used a separate timeout policy"


def test_safety_net_preserves_wip_and_next_run_resumes_it(
    git_history, tmp_path, monkeypatch, fake_processes,
):
    from foragent import orch
    from test_keep_unfinished_work import (
        BRANCH, CHANGES, _case, _expected_tree, _question, _tree, _write_changes,
    )

    # #270's offline fast-import adapter preserves actual WIP messages and trees.
    case = _case(git_history, tmp_path, monkeypatch, executor="codex", max_runtime_minutes=90,
                 stall_minutes=120)
    case.git.fetch()
    tree = record_tree(case, fake_processes)
    for row in tree:
        cpu(fake_processes, row.pid, 0)
    _write_changes({"worktree": case.store.worktree_path(1)}, stage=True)
    runtime_tick(case, fake_processes)
    case.clock[0] += timedelta(seconds=SAFETY_NET + 1)
    cpu(fake_processes, tree[-1].pid, 1000)
    case.store.update_run(1, last_progress_at=case.clock[0].isoformat())
    run_checks._append(case.store.log_path(1), "Still working at the safety net")
    runtime_tick(case, fake_processes)
    assert case.store.get_run(1).status == "finished", "The generous 24h safety net must still work"
    saved = git_command(case.root, "rev-parse", BRANCH)
    assert saved != case.base, "Safety-net termination discarded the partial implementation"
    assert _tree(case.root, saved) == _expected_tree(case), "Safety-net WIP changed the saved implementation"
    orch.retry(case.ctx, 1, hint="Continue the saved work")
    case.store.start_run(1, branch=BRANCH, worktree=case.store.worktree_path(1))
    observed = []

    def resume(kwargs):
        directory = kwargs["worktree"]
        observed.append({name: (directory / name).read_text(encoding="utf-8")
                         if (directory / name).exists() else None for name in CHANGES})
        _question(directory)

    # Only unrelated baseline launching is replaced; branch/worktree setup is real.
    monkeypatch.setattr("foragent.run_implementation._capture_acceptance_baseline", lambda *a, **kw: None)
    case.implementer.steps = [resume]
    assert runner.run_issue(1, case.ctx) == "spec-question"
    assert observed == [CHANGES], "The next executor started from zero instead of the safety-net WIP"
    assert len(case.implementer.calls) == 1
    row = assert_facts(case, "safety-net", age=0)
    assert row["cpu_delta"] > 0, "The safety net must record that the tree was still consuming CPU"
