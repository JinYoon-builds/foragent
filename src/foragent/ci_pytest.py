"""Standalone pytest measurement plugin, copied into each CI checkout's environment."""

import faulthandler
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest


def balanced(times, count):
    """Longest file first onto the least loaded shard (ties: path, then lowest index)."""
    loads, shard = [0.0] * count, {}
    for path, seconds in sorted(times.items(), key=lambda pair: (-pair[1], pair[0])):
        index = loads.index(min(loads))
        shard[path] = index
        loads[index] += seconds
    return shard


def read_times(path):
    """Per-file seconds from the trusted base, or {} when absent or unreadable."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
            and seconds >= 0 for key, seconds in value.items()):
        return {}
    return {key: float(seconds) for key, seconds in value.items()}


def shard_of(path, count, assignment):
    if path in assignment:
        return assignment[path]
    # Hash the file path, so a node changing parametrization stays on its file's shard.
    return int.from_bytes(sha256(path.encode()).digest(), "big") % count


class Measurement:
    def __init__(self):
        self.ids = []
        self.executed_ids = []
        self.cached_passes = set()
        self.call_passed = set()
        self.nonpassing = set()
        self.finished = set()
        self.collection_failed = False
        self.failed = False
        try:
            self.hang_seconds = int(os.environ.get("FA_TEST_HANG_SECONDS", "0"))
        except ValueError:
            self.hang_seconds = 0
        # pytest_configure runs with capture suspended. Keep stderr available
        # even when a hung test exits before pytest can replay captured output.
        self.stderr = os.dup(2) if self.hang_seconds > 0 else None

    def pytest_unconfigure(self, config):
        if self.stderr is not None:
            os.close(self.stderr)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        if self.hang_seconds > 0:
            faulthandler.dump_traceback_later(self.hang_seconds, file=self.stderr, exit=True)
        try:
            yield
        finally:
            if self.hang_seconds > 0:
                faulthandler.cancel_dump_traceback_later()

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items):
        index, count = int(os.environ["FA_SHARD"]), int(os.environ["FA_SHARDS"])
        # The assignment depends only on the base's times, never on what this side
        # collected, so base and head put every file in the same shard.
        assignment = balanced(read_times(os.environ.get("FA_SHARD_TIMES")), count)
        items[:] = [item for item in items
                    if shard_of(item.nodeid.split("::", 1)[0], count, assignment) == index]
        self.ids = [item.nodeid for item in items]
        cached = json.loads(Path(os.environ["FA_CACHED_RESULTS"]).read_text()) if "FA_CACHED_RESULTS" in os.environ else {}
        self.cached_passes = {node for node in self.ids if cached.get(node) == "passed"}
        items[:] = [item for item in items if item.nodeid not in cached]
        self.executed_ids = [item.nodeid for item in items]

    def pytest_collectreport(self, report):
        self.collection_failed |= report.failed

    def pytest_runtest_logreport(self, report):
        self.failed |= report.failed
        if report.when == "call" and report.passed:
            self.call_passed.add(report.nodeid)
        if not report.passed:
            self.nonpassing.add(report.nodeid)
        if report.when == "teardown":
            self.finished.add(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        expected = int(self.failed) if self.executed_ids else 5
        value = {"complete": int(exitstatus) == expected and not self.collection_failed
                             and self.finished == set(self.executed_ids),
                 "exit_code": expected,
                 "collected_ids": sorted(self.ids),
                 "passing_ids": sorted(self.cached_passes | (self.call_passed - self.nonpassing))}
        Path(os.environ["FA_REPORT"]).write_text(json.dumps(value), encoding="utf-8")


def pytest_configure(config):
    config.pluginmanager.register(Measurement(), "foragent-measurement")
