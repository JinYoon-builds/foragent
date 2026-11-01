"""Standalone pytest measurement plugin, copied into each CI checkout's environment."""

from hashlib import sha256
import json
import os
from pathlib import Path

import pytest


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

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items):
        index, count = int(os.environ["FA_SHARD"]), int(os.environ["FA_SHARDS"])
        # Hash the file path, so a node changing parametrization stays on its file's shard.
        items[:] = [item for item in items
                    if int.from_bytes(sha256(item.nodeid.split("::", 1)[0].encode()).digest(), "big") % count == index]
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
