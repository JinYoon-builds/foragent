"""Acceptance test for step-weights (#1635 part a): learn what each step of a run weighs.

The gate tick runs on a simulated machine. It learns the CPU and memory that each
step of a run (implementing, reviewing, checking) uses on this machine and this
project. It learns them from the run's own processes, and the learned weights are
shown in ``fa orch facts``. This part does not change the start rule.

Every outside fact the gate may read agrees with one model of the world:

- machine size: ``os.cpu_count``, ``psutil.cpu_count``, ``psutil.virtual_memory``;
- machine use: ``scheduler.machine_load``, ``psutil.virtual_memory().available``,
  ``os.getloadavg``;
- per-run use: each run is a row in the shared fake process table. Its local
  checks run in a child process of the run, so the heavy use is only visible in
  the run's process tree. ``psutil.Process(pid)`` reports each row's CPU time,
  CPU percent and memory. No real process is started or signalled;
- step: the run's recorded phase.

Time is the tick clock only. Names, units and the shape of the weights in facts
are the lane's choice; the test finds them by meaning.
"""

import os
from collections import namedtuple
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import psutil
import pytest
from test_skills_autosync import isolated_home  # noqa: F401  (fixture)

GB = 1024 ** 3
VMem = namedtuple("svmem", "total available percent used free active inactive wired")
CpuTimes = namedtuple("pcputimes", "user system children_user children_system")
MemInfo = namedtuple("pmem", "rss vms pfaults pageins")
MemFull = namedtuple("pfullmem", "rss vms pfaults pageins uss")

# One run: a long light step, a short review, then the heavy local checks.
STEPS = (("implementing", 60), ("reviewing", 30), ("checking", 30))
LIGHT_STEP = {"cpu": 0.2, "mem_gb": 0.4}
IDLE = {"cpu": 0.0, "mem_gb": 0.0}


class World:
    """One machine and one project's runs; the single source of every fact the gate reads."""

    def __init__(self, *, cpus, mem_gb, heavy_cpu, heavy_mem_gb, table, first_pid):
        self.cpus, self.mem = cpus, mem_gb * GB
        self.heavy = {"cpu": heavy_cpu, "mem_gb": heavy_mem_gb}
        self.runs = {}  # number -> {"pid", "child", "step", "left"}
        self.cpu_s = {}  # pid -> CPU seconds used so far
        self.table, self.next_pid, self.cwd = table, first_pid, None
        self.samples = []  # (minute, running, cpu demand, memory demand)

    def step(self, number):
        return STEPS[self.runs[number]["step"]][0]

    def use_of(self, pid):
        """What one process uses now: the runner is light, its check child is heavy."""
        for run in self.runs.values():
            if run["pid"] == pid:
                return LIGHT_STEP
            if run["child"] == pid:
                return self.heavy
        return IDLE

    def pids(self):
        return [pid for run in self.runs.values() for pid in (run["pid"], run["child"]) if pid is not None]

    def demand(self):
        uses = [self.use_of(pid) for pid in self.pids()]
        return sum(u["cpu"] for u in uses), sum(u["mem_gb"] for u in uses) * GB

    def known(self, pid):
        return pid in self.pids()

    def new_pid(self, start_time, ppid=1):
        """A new row in the fake process table; pids are never reused."""
        pid, self.next_pid = self.next_pid, self.next_pid + 1
        self.table.add(pid, ppid=ppid, cwd=self.cwd, start_time=start_time)
        self.cpu_s[pid] = 0.0
        return pid

    def end(self, pid):
        self.table.exit(pid)


def fake_process_class(world, real):
    class FakeProcess:
        def __init__(self, pid=None):
            if pid is None:  # The test process itself, read-only.
                self._real = real()
                self.pid = self._real.pid
                return
            self.pid, self._real = pid, None
            if not world.known(pid) and pid not in world.table.live:
                raise psutil.NoSuchProcess(pid)

        def __getattr__(self, name):
            if self._real is None:
                raise AttributeError(name)
            return getattr(self._real, name)

        def _use(self):
            return world.use_of(self.pid)

        @contextmanager
        def oneshot(self):
            yield

        def is_running(self):
            return self.pid in world.table.live

        def status(self):
            return psutil.STATUS_RUNNING if world.known(self.pid) else psutil.STATUS_SLEEPING

        def create_time(self):
            return world.table.rows[self.pid].start_time if self.pid in world.table.rows else 0.0

        def ppid(self):
            return world.table.rows[self.pid].ppid if self.pid in world.table.rows else 1

        def name(self):
            return "foragent-run"

        def cmdline(self):
            return ["fa", "gate", "run"]

        def children(self, recursive=False):
            found, frontier = [], [self.pid]
            while frontier:
                parent = frontier.pop()
                kids = [pid for pid in sorted(world.table.live) if world.table.rows[pid].ppid == parent]
                found.extend(kids)
                if recursive:
                    frontier.extend(kids)
            return [FakeProcess(pid) for pid in found]

        def cpu_times(self):
            return CpuTimes(world.cpu_s.get(self.pid, 0.0), 0.0, 0.0, 0.0)

        def cpu_percent(self, interval=None):
            return self._use()["cpu"] * 100.0

        def memory_info(self):
            rss = int(self._use()["mem_gb"] * GB)
            return MemInfo(rss, rss, 0, 0)

        def memory_full_info(self):
            rss = int(self._use()["mem_gb"] * GB)
            return MemFull(rss, rss, 0, 0, rss)

        def memory_percent(self, memtype="rss"):
            return 100.0 * self._use()["mem_gb"] * GB / world.mem

    return FakeProcess


@pytest.fixture
def machine(git_history, tmp_path, monkeypatch, isolated_home, fake_process_table):  # noqa: F811
    """Build a fresh simulated machine plus one repository with many ready lanes."""
    from conftest import git_command
    from test_orch_review_handoff import make_case, tick_context

    from foragent import capacity, scheduler
    from foragent.state import StateStore

    worlds = []

    def build(name, *, cpus, mem_gb, heavy_cpu=1.0, heavy_mem_gb=1.0, lanes=16, max_parallel=None):
        home = tmp_path / f"home-{name}"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        for key, directory in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                               ("XDG_CACHE_HOME", ".cache")):
            monkeypatch.setenv(key, str(home / directory))
        config_dir = home / ".config/foragent"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yml").write_text("skill_libraries: []\n")
        # Each simulated machine owns its data root: shared budgets and learned weights start empty.
        data = home / ".local/share/foragent"
        data.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(capacity, "machine_root", lambda data=data: data)
        if max_parallel is not None:
            (data / "machine.yml").write_text(f"max_parallel: {max_parallel}\n")

        world = World(cpus=cpus, mem_gb=mem_gb, heavy_cpu=heavy_cpu, heavy_mem_gb=heavy_mem_gb,
                      table=fake_process_table, first_pid=60000 + 10000 * len(worlds))
        worlds.append(world)
        directory = tmp_path / name
        directory.mkdir()
        clone = directory / "clone"
        world.cwd = clone
        git_command(directory, "clone", str(git_history.origin), str(clone))
        repo = f"owner/{name}"
        case = make_case(replace(git_history, clone=clone), directory, monkeypatch, repo=repo,
                         skill_libraries=[], test_command="true", max_runtime_minutes=240,
                         lanes={f"lane-{n}": {} for n in range(1, lanes + 1)})
        case.store = case.ctx.store = StateStore.for_repo(repo, now=case.ctx.now)
        case.ctx.repo = repo
        assert case.store.root.parent == home / ".local/share/foragent"
        body = case.tracker.get_issue(1).body
        case.body = body
        case.tracker.remove_labels(1, ["running", "approved"])
        case.lane_of = {}
        case.next_number = lanes + 1
        for number in range(1, lanes + 1):
            if number != 1:
                case.tracker.add_issue(number, f"Task {number}", body)
            case.tracker.add_labels(number, [f"lane:lane-{number}"])
            case.tracker.record_label(number, "approved", "human")
            case.lane_of[number] = f"lane-{number}"
        case.launched, case.killed, case.world = [], [], world

        def launch(number, case=case):
            case.launched.append(number)
            pid = world.new_pid(case.clock[0].timestamp())
            world.runs[number] = {"pid": pid, "child": None, "step": 0, "left": STEPS[0][1]}
            return pid

        case.tick_ctx = replace(tick_context(case), repo=repo, launcher=launch,
                                kill_runner=lambda pid, case=case: case.killed.append(pid))
        install_world(world, monkeypatch, capacity, scheduler)
        return case

    return build


def install_world(world, monkeypatch, capacity, scheduler):
    real = psutil.Process

    def load():
        cpu, mem = world.demand()
        pressure = "critical" if mem >= world.mem else "warn" if mem >= 0.85 * world.mem else "normal"
        return (pressure, cpu / world.cpus)

    def vmem():
        _, used = world.demand()
        available = max(0, world.mem - used)
        return VMem(world.mem, available, 100.0 * used / world.mem, used, available, used, 0, 0)

    monkeypatch.setattr(os, "cpu_count", lambda: world.cpus)
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: world.cpus)
    monkeypatch.setattr(psutil, "virtual_memory", vmem)
    monkeypatch.setattr(os, "getloadavg", lambda: (world.demand()[0],) * 3)
    monkeypatch.setattr(scheduler, "machine_load", load, raising=False)
    monkeypatch.setattr(capacity, "memory_pressure", lambda: load()[0])
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: load()[0])
    monkeypatch.setattr(psutil, "Process", fake_process_class(world, real))


def set_phase(case, number, phase):
    case.store.update_run(number, phase=phase, phase_started_at=case.clock[0].isoformat())


def tick(case):
    from foragent import scheduler

    before = set(case.world.runs)
    report = scheduler.tick(case.tick_ctx)
    assert report.skipped is None, report
    for number in set(case.world.runs) - before:
        set_phase(case, number, STEPS[0][0])
    return report


def enter_step(case, number):
    """A run's local checks start a child process; it ends when the checks end."""
    world, run = case.world, case.world.runs[number]
    if run["child"] is not None:
        world.end(run["child"])
        run["child"] = None
    phase = world.step(number)
    if phase == "checking":
        run["child"] = world.new_pid(case.clock[0].timestamp(), ppid=run["pid"])
    set_phase(case, number, phase)


def advance(case, minutes):
    """Move every run through its steps; a run that ends merges and its lane gets new work."""
    world = case.world
    for pid in world.pids():
        world.cpu_s[pid] += world.use_of(pid)["cpu"] * minutes * 60
    case.clock[0] += timedelta(minutes=minutes)
    for number in list(world.runs):
        run = world.runs[number]
        run["left"] -= minutes
        # A working runner reports progress, so the gate's stall rule leaves it alone.
        case.store.update_run(number, last_progress_at=case.clock[0].isoformat())
        if run["left"] > 0:
            continue
        run["step"] += 1
        if run["step"] < len(STEPS):
            run["left"] = STEPS[run["step"]][1]
            enter_step(case, number)
            continue
        if run["child"] is not None:
            world.end(run["child"])
        del world.runs[number]
        world.end(run["pid"])
        case.store.finish_run(number, outcome="merged")
        case.tracker.close_issue(number)
        case.tracker.remove_labels(number, ["running", "ready"])
        lane = case.lane_of.pop(number)
        new = case.next_number
        case.next_number += 1
        case.tracker.add_issue(new, f"Task {new}", case.body)
        case.tracker.add_labels(new, [f"lane:{lane}"])
        case.tracker.record_label(new, "approved", "human")
        case.lane_of[new] = lane


def simulate(case, hours, step_minutes=15):
    world = case.world
    for index in range(int(hours * 60 / step_minutes)):
        tick(case)
        cpu, mem = world.demand()
        world.samples.append((index * step_minutes, len(world.runs), cpu, mem))
        advance(case, step_minutes)


# --- reading the learned weights out of facts, by meaning only ---------------------------

STEP_WORDS = {"implementing": "implement", "reviewing": "review", "checking": "check"}


def find_weights(value):
    """The first mapping in facts whose keys name the three steps, each with CPU and memory numbers."""
    if isinstance(value, dict):
        named = {step: next((key for key in value if isinstance(key, str) and word in key.lower()), None)
                 for step, word in STEP_WORDS.items()}
        if all(named.values()) and all(_number(value[key], "cpu") is not None
                                       and _number(value[key], "mem") is not None for key in named.values()):
            return {step: value[key] for step, key in named.items()}
        for item in value.values():
            if (found := find_weights(item)) is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            if (found := find_weights(item)) is not None:
                return found
    return None


def _number(entry, word):
    if not isinstance(entry, dict):
        return None
    return next((v for k, v in entry.items() if word in str(k).lower()
                 and isinstance(v, (int, float)) and not isinstance(v, bool)), None)


def cpus(entry):
    """CPU weight in CPUs; a percent of one CPU (100 = one CPU) is accepted too."""
    value = float(_number(entry, "cpu"))
    return value / 100.0 if value > 64 else value


def memory_gb(entry, scale):
    return float(_number(entry, "mem")) / scale


def memory_scale(entry, expected_gb):
    """The unit the lane chose (bytes, KB, MB or GB): the one that puts the value nearest the truth."""
    value = float(_number(entry, "mem"))
    return min((GB, 1024 ** 2, 1024, 1), key=lambda scale: abs(value / scale - expected_gb))


def learned(case):
    from foragent import orch_facts

    facts = orch_facts.collect(case.store, case.tick_ctx.config, case.root)
    weights = find_weights(facts)
    assert weights is not None, (
        "fa orch facts must show a learned CPU and memory weight for implementing, reviewing and checking"
    )
    return weights


@pytest.mark.parametrize("project,heavy", [("heavy", 6.0), ("light", 1.0)])
def test_step_weights_are_learned_from_each_runs_process_tree(machine, project, heavy):
    """The weights are what the runs' processes used in each step on this machine and project.

    The checks' load is only in a child process of the run, so a reader of the run's
    own pid alone learns the wrong check weight. A light and a heavy project on the
    same machine learn different check weights: the weight is measured, not a constant.
    """
    case = machine(project, cpus=16, mem_gb=32, heavy_cpu=heavy, heavy_mem_gb=heavy)
    simulate(case, hours=6)

    weights = learned(case)
    check, implement, review = weights["checking"], weights["implementing"], weights["reviewing"]
    # Checking: the light runner plus its heavy child.
    expected_check = heavy + LIGHT_STEP["cpu"]
    assert 0.75 * expected_check <= cpus(check) <= 1.25 * expected_check, (
        f"The {project} project's checks use {expected_check} CPUs (runner and its check child); "
        f"facts show {cpus(check)}"
    )
    scale = memory_scale(check, heavy + LIGHT_STEP["mem_gb"])
    expected_mem = heavy + LIGHT_STEP["mem_gb"]
    assert 0.75 * expected_mem <= memory_gb(check, scale) <= 1.25 * expected_mem, (
        f"The {project} project's checks use {expected_mem} GB; facts show {_number(check, 'mem')}"
    )
    for name, entry in (("implementing", implement), ("reviewing", review)):
        assert cpus(entry) <= 2 * LIGHT_STEP["cpu"], f"{name} uses {LIGHT_STEP['cpu']} CPU; facts show {cpus(entry)}"
        assert memory_gb(entry, scale) <= 2 * LIGHT_STEP["mem_gb"], (
            f"{name} uses {LIGHT_STEP['mem_gb']} GB; facts show {_number(entry, 'mem')} (same unit as checking)"
        )


def test_step_weights_survive_a_new_gate_and_belong_to_this_machine(machine):
    """A later gate (new store objects, no runs at all) still shows what this machine learned.

    A second, fresh machine has learned nothing yet: its facts carry no measured check weight.
    """
    from foragent.state import StateStore

    case = machine("learned", cpus=16, mem_gb=32, heavy_cpu=6.0, heavy_mem_gb=6.0)
    simulate(case, hours=6)
    for number in list(case.world.runs):  # Every run ends; nothing is running any more.
        run = case.world.runs.pop(number)
        for pid in (run["child"], run["pid"]):
            if pid is not None:
                case.world.end(pid)
        case.store.finish_run(number, outcome="merged")
        case.tracker.close_issue(number)
    case.store = case.ctx.store = StateStore.for_repo(case.ctx.repo, now=case.ctx.now)

    weights = learned(case)
    assert cpus(weights["checking"]) >= 4.5, (
        f"The learned check weight must stay after its runs end and a new gate starts: {weights['checking']}"
    )

    fresh = machine("fresh", cpus=16, mem_gb=32, heavy_cpu=6.0, heavy_mem_gb=6.0)
    from foragent import orch_facts

    facts = orch_facts.collect(fresh.store, fresh.tick_ctx.config, fresh.root)
    weights = find_weights(facts)
    assert weights is None or not _number(weights["checking"], "cpu"), (
        f"A machine that ran nothing has measured nothing; it must not show a learned check weight: {weights}"
    )
