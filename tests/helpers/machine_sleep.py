"""Shared offline helpers for unit tests."""

from datetime import datetime, timedelta
import os

import pytest

from foragent import capacity, scheduler
from foragent.state import StateStore

from helpers.review_case import make_case, tick_context


_PID = 88001


class _ModuleView:
    def __init__(self, module, **overrides):
        self.module, self.overrides = module, overrides

    def __getattr__(self, name):
        return self.overrides[name] if name in self.overrides else getattr(self.module, name)


def _forbidden(*args, **kwargs):
    pytest.fail("Machine-sleep acceptance must not sleep, launch workers, or run sensor subprocesses")


class _SleepFacts:
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


def sleep_case(git_history, tmp_path, monkeypatch, **config):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
                     **{"max_runtime_minutes": 90, "stall_minutes": 3, **config})
    case.facts = _SleepFacts(case.clock)
    case.killed = []
    case.store.update_run(1, pid=_PID)
    case.ctx.executor_factory = _forbidden

    def probe(pid, sig):
        assert (pid, sig) == (_PID, 0), "Only the fixture runner may be probed"

    monkeypatch.setattr(scheduler, "os", _ModuleView(os, kill=probe))
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
    ctx = tick_context(case, start_runs=False, baseline_launcher=_forbidden)
    ctx.launcher, ctx.kill_runner = _forbidden, case.killed.append
    report = scheduler.tick(ctx)
    assert report.skipped is None, report
    assert not case.implementer.calls and not case.reviewer.calls, "No model turns are allowed"
    return report
