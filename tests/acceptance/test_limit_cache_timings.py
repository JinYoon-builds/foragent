"""Locked acceptance checks for limit-cache-timings; legacy cache hits keep their measurement."""

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


@pytest.mark.parametrize("wrapped,measured,other_duration,expected", [(False, 736, 100, 1472), (True, 810, 2000, 1620)])
def test_legacy_cache_hit_uses_its_junit_instead_of_another_selection(
    run_case, monkeypatch, wrapped, measured, other_duration, expected,
):
    c = run_case
    c.ctx.config = replace(c.ctx.config, checks="local", test_command="pytest -q",
                           max_runtime_minutes=360, max_retries=0)
    c.history.update({NODE.split("::")[0]: "def test_keep(): pass\n"})
    captured, protected, published = [], [], []

    @contextmanager
    def slot(*args, **kwargs):
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

    c.ctx.git.fetch("origin", "main")
    baseline = capture_acceptance_baseline(1, c.ctx, c.ctx.git, "main", "origin/main")
    assert baseline.node_ids == (NODE,) and len(captured) == 1
    cache = c.store.root.parent / "baselines"
    record, = cache.glob("*.json")
    saved = json.loads(record.read_text())
    del saved["duration_seconds"]  # The producer before #1347 did not record this field.
    record.write_text(json.dumps(saved))
    suite = ET.Element("testsuite", time=str(measured))
    ET.SubElement(suite, "testcase", file=NODE.split("::")[0], name="test_keep", time=str(measured))
    xml = ET.Element("testsuites") if wrapped else suite
    if wrapped:
        xml.append(suite)
    ET.ElementTree(xml).write(record.with_suffix(".xml"), encoding="unicode")
    # A newer, different selection on this same machine/repository must not supply the limit.
    c.now[0] += timedelta(seconds=1)
    other = {**saved, "base_sha": "another-base", "node_ids": ["tests/acceptance/test_other.py::test_other"],
             "collected_ids": ["tests/acceptance/test_other.py::test_other"],
             "duration_seconds": other_duration, "created_at": c.now[0].isoformat()}
    (cache / "another-selection.json").write_text(json.dumps(other))

    outcome = runner.run_issue(1, c.ctx)
    assert outcome == "checked", c.store.log_path(1).read_text()
    assert len(captured) == len(c.executor.calls) == len(published) == 1, "Reuse the selected cached baseline once"
    assert protected == [expected], (
        f"Cached selection's JUnit records {measured}s; unrelated {other_duration}s selection "
        f"must not replace its {expected}s allowance: got {protected}"
    )
