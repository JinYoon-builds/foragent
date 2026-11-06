"""Exact owner pass selection, with a receipt for an entirely credited pytest session."""

import json
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--fa-pass-skips")
    parser.addoption("--fa-pass-root")
    parser.addoption("--fa-pass-receipt")


def pytest_configure(config):
    passed = json.loads(Path(config.getoption("--fa-pass-skips")).read_text(encoding="utf-8"))
    root = Path(config.getoption("--fa-pass-root") or config.rootpath).resolve()
    receipt = Path(config.getoption("--fa-pass-receipt"))
    config.pluginmanager.register(PassSelection(set(passed), root, receipt), "foragent-pass-selection")


class PassSelection:
    def __init__(self, passed, root, receipt):
        self.passed, self.root, self.receipt = passed, root, receipt
        self.removed_all = False
        self.nodes, self.called, self.nonpassing = {}, set(), set()

    def node_id(self, item):
        # Done when can discover a parent config or cd into a subdirectory. CI ids are
        # relative to the checked checkout, independently of pytest's discovered root.
        try:
            path = item.path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None
        _, separator, name = item.nodeid.partition("::")
        return f"{path}::{name}" if separator else None

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, config, items):
        yield
        # Apply after the command's own selectors: an empty -k/-m selection is still exit 5.
        self.nodes = {item.nodeid: self.node_id(item) for item in items}
        removed = [item for item in items if self.nodes[item.nodeid] in self.passed]
        self.removed_all = bool(items) and len(removed) == len(items)
        items[:] = [item for item in items if self.nodes[item.nodeid] not in self.passed]
        config.hook.pytest_deselected(items=removed)

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            self.called.add(report.nodeid)
        if not report.passed:
            self.nonpassing.add(report.nodeid)
        if (report.when == "teardown" and report.nodeid in self.called
                and report.nodeid not in self.nonpassing and (node := self.nodes.get(report.nodeid))):
            with self.receipt.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(node) + "\n")

    def pytest_sessionfinish(self, session, exitstatus):
        if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and self.removed_all:
            session.exitstatus = pytest.ExitCode.OK
