"""Locked acceptance checks for limit-exclude-slot-wait; a queue is not test execution."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json
import shlex
import xml.etree.ElementTree as ET

import pytest

from foragent import capacity, run_baseline, runner
from foragent.run_implementation import capture_acceptance_baseline
from runner_support import run_case  # noqa: F401


NODE = "tests/acceptance/test_keep.py::test_keep"


@pytest.mark.parametrize("measured,queued,expected", [(736, 400, 1472), (810, 1600, 1620)])
def test_slot_wait_does_not_inflate_fresh_or_cached_protected_limit(run_case, monkeypatch, measured, queued, expected):
    c = run_case
    c.ctx.config = replace(c.ctx.config, checks="local", test_command="pytest -q",
                           max_runtime_minutes=360, max_retries=0)
    c.history.update({NODE.split("::")[0]: "def test_keep(): pass\n"})
    captured, protected, published, acquisitions = [], [], [], []

    @contextmanager
    def slot(*args, waiting=None, **kwargs):
        acquisitions.append(1)
        if len(acquisitions) == 1:
            assert waiting is not None, "Exercise the real runner's credited slot queue"
            waiting()
            c.now[0] += timedelta(seconds=queued)
            waiting()
        yield

    def process(argv, *, cwd, timeout_s, log_path, **kwargs):
        words = shlex.split(argv[-1])
        report = next((word.split("=", 1)[1] for word in words if word.startswith("--junitxml=")), None)
        if report is not None:
            captured.append(words)
            c.now[0] += timedelta(seconds=measured)
            suite = ET.Element("testsuite", time=str(measured))
            ET.SubElement(suite, "testcase", file=NODE.split("::")[0], name="test_keep", time=str(measured))
            ET.ElementTree(suite).write(report, encoding="unicode")
        elif NODE in words:
            protected.append(timeout_s)
            c.now[0] += timedelta(seconds=600)
        return 0, False, ""

    def publish(attempt):
        published.append(1)
        c.tracker.remove_labels(1, ["running"])
        return "checked"

    monkeypatch.setattr(capacity, "hold_test_slot", slot)
    monkeypatch.setattr(runner, "run_logged", process)
    monkeypatch.setattr(run_baseline, "monotonic", c.ctx.monotonic)
    monkeypatch.setattr(runner, "_publish", publish)
    outcome = runner.run_issue(1, c.ctx)
    assert outcome == "checked", c.store.log_path(1).read_text()
    assert c.store.get_run(1).waited_seconds == queued
    cached = capture_acceptance_baseline(1, c.ctx, c.ctx.git, "main", "origin/main")
    assert cached.node_ids == (NODE,)
    assert len(captured) == len(c.executor.calls) == len(published) == 1
    assert protected == [expected], (
        f"{measured}s of execution must give a {expected}s protected limit even after {queued}s "
        f"waiting for a slot: got {protected}"
    )
    assert cached.duration_seconds == measured, "Cache hits must retain execution time without the queue"
    record, = (c.store.root.parent / "baselines").glob("*.json")
    assert json.loads(record.read_text())["duration_seconds"] == measured
