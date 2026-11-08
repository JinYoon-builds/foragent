"""Fake process table and gate/facts drivers for the fseventsd resource facts acceptance tests.

Only fake processes are observed: no real process is listed, measured or signalled.
"""

from __future__ import annotations

from collections import namedtuple
from contextlib import contextmanager
from datetime import timedelta
import json
import os
import platform
import re
import sys
from types import SimpleNamespace

import psutil

from test_orch_facts import facts_case
from test_skills_autosync import cli_exit

FSEVENTSD_PID = 321
RSS_BYTES = 512 * 1024 * 1024  # 512 MiB resident
CPU_PERCENT = 37.5
SAMPLE_AGE_SECONDS = 45

Memory = namedtuple("Memory", "rss vms")
Times = namedtuple("Times", "user system children_user children_system")


class FakeProcess:
    """A psutil.Process stand-in that records any signal instead of delivering it."""

    def __init__(self, table, pid, name, rss, cpu, *, denied=False):
        self.table, self.pid, self._name, self.rss, self.cpu, self.denied = table, pid, name, rss, cpu, denied
        self.info = {}

    def _guard(self):
        if self.denied:
            raise psutil.AccessDenied(self.pid, self._name)

    def name(self):
        return self._name

    def exe(self):
        return f"/System/Library/Frameworks/CoreServices.framework/Versions/A/Support/{self._name}"

    def cmdline(self):
        return [self.exe()]

    def username(self):
        return "root"

    def status(self):
        return psutil.STATUS_RUNNING

    def ppid(self):
        return 1

    def create_time(self):
        return 1_700_000_000.0

    def is_running(self):
        return True

    def memory_info(self):
        self._guard()
        return Memory(self.rss, self.rss * 4)

    def memory_full_info(self):
        self._guard()
        return SimpleNamespace(rss=self.rss, vms=self.rss * 4, uss=self.rss, pfaults=0, pageins=0)

    def memory_percent(self, memtype="rss"):
        self._guard()
        return 3.125

    def cpu_percent(self, interval=None):
        self._guard()
        return self.cpu

    def cpu_times(self):
        self._guard()
        return Times(10.0, 5.0, 0.0, 0.0)

    def num_threads(self):
        return 4

    @contextmanager
    def oneshot(self):
        yield

    def as_dict(self, attrs=None, ad_value=None):
        names = attrs or ("pid", "name", "memory_info", "cpu_percent", "cpu_times", "exe", "username")
        result = {}
        for attr in names:
            try:
                value = getattr(self, attr)
                result[attr] = value() if callable(value) else value
            except psutil.AccessDenied:
                result[attr] = ad_value
        return result

    def send_signal(self, sig):
        self.table.signals.append((self.pid, sig))

    def terminate(self):
        self.table.signals.append((self.pid, "terminate"))

    def kill(self):
        self.table.signals.append((self.pid, "kill"))

    def suspend(self):
        self.table.signals.append((self.pid, "suspend"))

    def resume(self):
        self.table.signals.append((self.pid, "resume"))


class ProcessTable:
    """The process table every psutil and os entry point sees during a test."""

    def __init__(self, monkeypatch):
        self.processes: list[FakeProcess] = []
        self.signals: list[tuple[int, object]] = []
        real_process, real_kill = psutil.Process, os.kill

        def process_iter(attrs=None, ad_value=None):
            for process in list(self.processes):
                if attrs is not None:
                    process.info = process.as_dict(attrs, ad_value)
                yield process

        def process(pid=None):
            for item in self.processes:
                if item.pid == pid:
                    return item
            if pid == FSEVENTSD_PID:
                raise psutil.NoSuchProcess(pid)
            return real_process(pid) if pid is not None else real_process()

        def kill(pid, sig):
            if sig == 0:  # A liveness probe delivers nothing.
                return real_kill(pid, 0)
            self.signals.append((pid, sig))

        def killpg(pgid, sig):
            self.signals.append((-pgid, sig))

        monkeypatch.setattr(psutil, "process_iter", process_iter)
        monkeypatch.setattr(psutil, "Process", process)
        monkeypatch.setattr(psutil, "pids", lambda: [item.pid for item in self.processes])
        monkeypatch.setattr(psutil, "pid_exists", lambda pid: any(item.pid == pid for item in self.processes))
        monkeypatch.setattr(os, "kill", kill)
        monkeypatch.setattr(os, "killpg", killpg)

    def set(self, *, fseventsd=True, rss=RSS_BYTES, cpu=CPU_PERCENT, denied=False):
        self.processes = [
            FakeProcess(self, 1, "launchd", 40 * 1024 * 1024, 0.5),
            FakeProcess(self, 88, "mds_stores", 900 * 1024 * 1024, 12.0),
        ]
        if fseventsd:
            self.processes.insert(1, FakeProcess(self, FSEVENTSD_PID, "fseventsd", rss, cpu, denied=denied))


def case_with_table(tmp_path, monkeypatch):
    """facts_case with the fake process table installed before any sample is written."""
    table = ProcessTable(monkeypatch)
    table.set(fseventsd=False)
    case = facts_case(tmp_path, monkeypatch)
    case.table = table
    return case


def gate_tick(case, monkeypatch, *, macos=True):
    """One gate tick's budget step: the gate is the sole writer of the resource sample."""
    from foragent import capacity, resource_sample, scheduler, scheduler_start

    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "darwin" if macos else "linux")
        patch.setattr(platform, "system", lambda: "Darwin" if macos else "Linux")
        # Machine totals stay deterministic; only the process observation is under test.
        patch.setattr(capacity, "kernel_pressure", lambda: "normal")
        patch.setattr(capacity, "swapins_total", lambda: None)
        patch.setattr(capacity, "load_per_cpu", lambda: 0.2)
        patch.setenv("FORAGENT_CPU_BUSY", "off")
        patch.setattr(scheduler, "update_test_slots", lambda *args, **kwargs: None)
        ctx = SimpleNamespace(store=case.store, now=lambda: case.clock[0])
        scheduler_start._apply_budgets(ctx, {}, workers=False)
    return resource_sample.path_for(case.store)


def run_facts(case, capsys, *, human=False):
    """`fa orch facts [--json]` through the CLI; returns (exit code, stdout, stderr)."""
    capsys.readouterr()
    argv = ["orch", "facts", "--repo-root", str(case.root)] + ([] if human else ["--json"])
    code = cli_exit(case.main, argv)
    output = capsys.readouterr()
    return code, output.out, output.err


def advance(case, seconds=SAMPLE_AGE_SECONDS):
    case.clock[0] += timedelta(seconds=seconds)


# --- Locating the fseventsd reading in the JSON facts without fixing its schema ------------------------

def _mentions_fseventsd(node) -> bool:
    if isinstance(node, str):
        return "fseventsd" in node.lower()
    if isinstance(node, dict):
        return any("fseventsd" in str(key).lower() or _mentions_fseventsd(value) for key, value in node.items())
    if isinstance(node, list):
        return any(_mentions_fseventsd(value) for value in node)
    return False


def fseventsd_reading(facts):
    """(subtree, ancestors): the smallest dict that names fseventsd, and the dicts above it."""
    found = []

    def walk(node, ancestors):
        if isinstance(node, dict):
            for key, value in node.items():
                if "fseventsd" in str(key).lower() and isinstance(value, (dict, type(None), str)):
                    found.append((value if isinstance(value, dict) else {key: value}, ancestors + [node]))
                    return
            if any(isinstance(value, str) and "fseventsd" in value.lower() for value in node.values()):
                found.append((node, ancestors))
                return
            for value in node.values():
                walk(value, ancestors + [node])
        elif isinstance(node, list):
            for value in node:
                walk(value, ancestors)

    walk(facts, [])
    assert found, "fa orch facts --json does not identify the fseventsd process anywhere"
    return found[0]


def leaves(node, path=()):
    """Every (key path, value) leaf under node."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, path + (str(key).lower(),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from leaves(value, path + (str(index),))
    else:
        yield path, node


def _strings(node):
    return " ".join(str(value).lower() for _, value in leaves(node) if isinstance(value, str))


def _number(value):
    return type(value) in (int, float)


def memory_with_unit(subtree):
    """The resident memory in bytes (or MiB) with its unit named by the key or a unit field."""
    units = _strings(subtree)
    for path, value in leaves(subtree):
        key = "/".join(path)
        if not _number(value):
            continue
        if value == RSS_BYTES and ("byte" in key or "bytes" in units):
            return True
        if value == 512 and ("mib" in key or "mb" in key or "mib" in units):
            return True
    return False


def cpu_with_unit(subtree):
    """The CPU usage as a percentage (or fraction) with its unit named by the key or a unit field."""
    units = _strings(subtree)
    for path, value in leaves(subtree):
        key = "/".join(path)
        if not _number(value) or "mem" in key or "rss" in key:
            continue
        if value == CPU_PERCENT and ("percent" in key or "pct" in key or "percent" in units or "%" in units):
            return True
        if value == CPU_PERCENT / 100 and ("fraction" in key or "ratio" in key or "fraction" in units):
            return True
    return False


def sample_age(subtree, ancestors):
    """The age in seconds stated for this reading: on it, or on the sample that contains it."""
    for node in [subtree, *reversed(ancestors)]:
        items = list(leaves(subtree)) if node is subtree else [((str(k).lower(),), v) for k, v in node.items()]
        for path, value in items:
            if _number(value) and re.search(r"(^|[_/])age($|[_/])", "/".join(path)):
                return value
    return None


def numeric_zero_readings(subtree):
    """Memory or CPU leaves reported as a measured zero."""
    return [(path, value) for path, value in leaves(subtree) if _number(value) and value == 0
            and any(word in "/".join(path) for word in ("rss", "mem", "byte", "cpu", "percent", "pct"))]


UNAVAILABLE_WORDS = ("unavailable", "absent", "not running", "not found", "missing", "inaccessible",
                     "denied", "unsupported", "unknown", "not supported")


def unavailable_reading(subtree) -> bool:
    """An explicit unavailable reading: a None measurement or a word saying so."""
    words = _strings(subtree)
    if any(word in words for word in UNAVAILABLE_WORDS):
        return True
    return any(value is None and any(word in "/".join(path) for word in ("rss", "mem", "byte", "cpu"))
               for path, value in leaves(subtree))


def facts_json(text):
    return json.loads(text)


HUMAN_MEMORY = re.compile(r"512(\.0+)?\s*(MiB|MB|M)\b|0\.5\s*GiB|536870912\s*(B|bytes)")
HUMAN_CPU = re.compile(r"37\.5\s*%|38\s*%")


def fseventsd_line(human_text):
    lines = [line for line in human_text.splitlines() if "fseventsd" in line.lower()]
    assert lines, f"fa orch facts does not name fseventsd:\n{human_text}"
    return " ".join(lines)
