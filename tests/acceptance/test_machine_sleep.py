"""Acceptance test for machine-sleep. Locked by Foragent; do not edit."""

import ctypes
from datetime import datetime, timedelta
import os
import subprocess
from types import SimpleNamespace
import time

import pytest

from conftest import BASE_TIME
from foragent import capacity, scheduler
from foragent.reasons import Reason
from foragent.state import StateStore
from test_orch_review_handoff import make_case, tick_context


# Public contract: slept_seconds(since: aware datetime) -> nonnegative seconds.
# Calling it at the recorded point can establish a clock baseline; reads do not
# consume sleep credit. Wake policy chosen here: >60 s sleep, then 5 min grace.
WAKE_THRESHOLD_SECONDS = 60
WAKE_GRACE_SECONDS = 5 * 60
NIGHT = 8 * 60 * 60
PID = 88001


class ModuleView:
    def __init__(self, module, **overrides):
        self.module, self.overrides = module, overrides

    def __getattr__(self, name):
        return self.overrides[name] if name in self.overrides else getattr(self.module, name)


def forbidden(*args, **kwargs):
    pytest.fail("Machine-sleep acceptance must not sleep, launch workers, or run sensor subprocesses")


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", forbidden)


class SleepFacts:
    """Two fake clocks and observed sleep intervals; no scheduler decisions."""

    def __init__(self, clock):
        self.clock = clock
        self.awake = 1000.0
        self.boot = 1100.0  # Earlier sleep must not be credited to this run.
        self.intervals = []

    def advance(self, seconds, *, asleep=False):
        start = self.clock[0]
        self.clock[0] += timedelta(seconds=seconds)
        self.boot += seconds
        if asleep:
            self.intervals.append((start, self.clock[0]))
        else:
            self.awake += seconds

    def slept_seconds(self, since):
        assert isinstance(since, datetime) and since.utcoffset() is not None, (
            "slept_seconds(since) takes a timezone-aware recorded datetime"
        )
        return sum(max(0.0, (end - max(start, since)).total_seconds())
                   for start, end in self.intervals)


def fake_sensor_inputs(monkeypatch, platform):
    facts = SleepFacts([BASE_TIME])
    calls = []
    monotonic_id, boottime_id = 1, 7

    def gettime(clock_id):
        calls.append(clock_id)
        if clock_id == monotonic_id:
            return facts.awake
        if clock_id == boottime_id and platform == "linux":
            return facts.boot
        raise OSError("Clock unavailable")

    def monotonic():
        calls.append(monotonic_id)
        return facts.boot if platform == "darwin" else facts.awake

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return facts.clock[0].astimezone(tz) if tz else facts.clock[0].replace(tzinfo=None)

    class Timeval(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_int)]

    def sysctlbyname(name, output, size, new, new_size):
        name = name if isinstance(name, bytes) else name.value
        calls.append(name)
        if platform != "darwin" or name not in {b"kern.sleeptime", b"kern.waketime"}:
            return -1
        interval = facts.intervals[-1] if facts.intervals else (
            BASE_TIME - timedelta(hours=20), BASE_TIME - timedelta(hours=12),
        )
        timestamp = interval[name == b"kern.waketime"].timestamp()
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value = ctypes.sizeof(Timeval)
        if output is not None:
            value = ctypes.cast(output, ctypes.POINTER(Timeval)).contents
            value.tv_sec, value.tv_usec = int(timestamp), 0
        return 0

    monkeypatch.setattr(capacity, "sys", ModuleView(capacity.sys, platform=platform))
    monkeypatch.setattr(capacity, "datetime", Clock)
    monkeypatch.setattr(capacity, "time", ModuleView(
        time, time=lambda: facts.clock[0].timestamp(),
        # Darwin monotonic can include sleep: only kernel facts disambiguate it.
        monotonic=monotonic, monotonic_ns=lambda: int(monotonic() * 1e9),
        clock_gettime=gettime, clock_gettime_ns=lambda clock_id: int(gettime(clock_id) * 1e9),
        CLOCK_MONOTONIC=monotonic_id, CLOCK_BOOTTIME=boottime_id,
    ))
    monkeypatch.setattr(capacity, "ctypes", ModuleView(
        ctypes, CDLL=lambda *args, **kwargs: SimpleNamespace(sysctlbyname=sysctlbyname),
    ))
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    return facts, calls


@pytest.mark.parametrize("platform", ["darwin", "linux", "fallback"])
def test_slept_seconds_reads_platform_facts_without_subprocesses(monkeypatch, platform):
    facts, calls = fake_sensor_inputs(monkeypatch, platform)
    sensor = getattr(capacity, "slept_seconds", None)
    assert callable(sensor), "Missing public sensor: foragent.capacity.slept_seconds(since)"
    since = facts.clock[0]
    assert sensor(since) == 0, "Sleep before the recorded point must not count"
    facts.advance(60)
    assert sensor(since) == 0, "Ordinary awake time must not become sleep credit"
    facts.advance(NIGHT, asleep=True)
    facts.advance(30 * 60)
    assert sensor(since) == pytest.approx(NIGHT), f"{platform}: distinguish 8 h sleep from 30 min work"
    assert sensor(since) == pytest.approx(NIGHT), "Reading the sensor must not consume credit"
    assert sensor(facts.clock[0]) == 0, "A point recorded after wake must not inherit old sleep"
    if platform == "darwin":
        assert {b"kern.sleeptime", b"kern.waketime"} <= set(calls)
    elif platform == "linux":
        assert {1, 7} <= set(calls), "Linux must compare CLOCK_BOOTTIME and CLOCK_MONOTONIC"


def sleep_case(git_history, tmp_path, monkeypatch, **config):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
                     **{"max_runtime_minutes": 90, "stall_minutes": 3, **config})
    case.facts = SleepFacts(case.clock)
    case.killed = []
    case.store.update_run(1, pid=PID)
    case.ctx.executor_factory = forbidden

    def probe(pid, sig):
        assert (pid, sig) == (PID, 0), "Only the fixture runner may be probed"

    monkeypatch.setattr(scheduler, "os", ModuleView(os, kill=probe))
    monkeypatch.setattr(scheduler, "machine_load", lambda: capacity.LoadSample("normal", 0, 0))
    # raising=False lets today's scheduler run far enough to expose the actual
    # timeout/stall defect rather than failing solely on an absent sensor API.
    monkeypatch.setattr(capacity, "slept_seconds", case.facts.slept_seconds, raising=False)
    tick_case(case)
    return case


def tick_case(case):
    # A periodic gate gets fresh context/store objects; wake grace cannot live
    # only on a TickContext that disappears at the end of the first wake tick.
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    ctx = tick_context(case, start_runs=False, baseline_launcher=forbidden)
    ctx.launcher, ctx.kill_runner = forbidden, case.killed.append
    report = scheduler.tick(ctx)
    assert report.skipped is None, report
    assert not case.implementer.calls and not case.reviewer.calls, "No model turns are allowed"
    return report


def assert_running(case, message):
    run = case.store.get_run(1)
    assert run.status == "running" and not case.killed and case.store.load_flag(1) is None, (
        f"{message}; status={run.status}, outcome={run.outcome}, killed={case.killed}"
    )
    assert case.store.read_history() == [], "Sleep must not create a failed run history row"
    assert case.tracker.get_issue(1).labels & {"running", "ready", "blocked"} == {"running"}


@pytest.mark.parametrize("waited_minutes", [0, 60])
def test_sleep_and_wait_credit_preserve_the_runtime_limit(
    git_history, tmp_path, monkeypatch, waited_minutes,
):
    # Exercise the unchanged no-sleep limit before the new sleep scenario, so
    # both regression checks run even though every test must be red on main.
    for sleeping in (False, True):
        with monkeypatch.context() as patch:
            case = sleep_case(git_history, tmp_path / str(sleeping), patch, stall_minutes=2000)
            case.facts.advance((waited_minutes + 30) * 60)
            case.store.update_run(1, waited_seconds=waited_minutes * 60,
                                  last_progress_at=case.clock[0].isoformat())
            if sleeping:
                case.facts.advance(NIGHT, asleep=True)
            tick_case(case)
            assert_running(case, "8 h sleep plus 30 min work must fit a 90 min runtime limit")
            tick_case(case)
            assert_running(case, "Repeated ticks must not count sleep as runtime")
            case.facts.advance(60 * 60)  # Exactly 90 min awake work, plus credited waits.
            case.store.update_run(1, last_progress_at=case.clock[0].isoformat())
            if sleeping:
                case.facts.advance(NIGHT, asleep=True)
            tick_case(case)
            assert_running(case, "A second sleep must preserve the same runtime budget")
            # Preserve the gate's existing extra 10 min backstop allowance.
            case.facts.advance(11 * 60)
            case.store.update_run(1, last_progress_at=case.clock[0].isoformat())
            tick_case(case)
            run = case.store.get_run(1)
            assert run.status == "finished" and run.outcome == "killed", (
                "101 min of real work must still exceed the 90 min gate runtime backstop"
            )
            assert case.killed == [PID] and case.store.load_flag(1).reason == Reason.TIMEOUT


def test_sleep_is_not_stall_time_and_no_sleep_still_stalls(git_history, tmp_path, monkeypatch):
    for sleeping in (False, True):
        with monkeypatch.context() as patch:
            # Keep runtime out of this test, isolating the stall detector.
            case = sleep_case(git_history, tmp_path / str(sleeping), patch, max_runtime_minutes=2000)
            case.facts.advance(30)  # Fresh progress just before suspend.
            case.store.update_run(1, last_progress_at=case.clock[0].isoformat())
            if sleeping:
                case.facts.advance(NIGHT, asleep=True)
            tick_case(case)
            assert_running(case, "A quiet sleeping runner must not be stopped as stalled")
            # Activity ends grace. After one awake minute, the old sleep still
            # must not be charged to the new last_progress_at interval.
            case.facts.advance(1)
            case.store.update_run(1, last_progress_at=case.clock[0].isoformat())
            case.facts.advance(60)
            tick_case(case)
            assert_running(case, "Only awake silence since progress belongs to the stall clock")
            case.facts.advance(3 * 60)
            tick_case(case)
            assert case.killed == [PID] and case.store.get_run(1).outcome == "stalled-restart", (
                "Four minutes of real silence must still trigger the existing first-stall restart"
            )
            assert "ready" in case.tracker.get_issue(1).labels
