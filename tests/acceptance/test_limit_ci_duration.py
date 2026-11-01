"""Locked acceptance checks for limit-ci-duration. Run through the issue runner."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import shlex
import xml.etree.ElementTree as ET

import pytest

from foragent import capacity, run_baseline, runner
from runner_support import run_case  # noqa: F401


NODE = "tests/acceptance/test_machine.py::test_machine"


@pytest.mark.parametrize("measured,expected", [(736, 1472), (1200, 2400)])
def test_ci_protected_check_uses_its_local_selection_duration(run_case, monkeypatch, measured, expected):
    c = run_case
    c.ctx.config = replace(c.ctx.config, checks="ci", test_command="pytest -q",
                           max_runtime_minutes=360, max_retries=0)
    c.history.update({NODE.split("::")[0]:
                      "import pytest\n@pytest.mark.local_only\ndef test_machine(): pass\n"})
    captured, protected, implemented, published = [], [], [], []

    @contextmanager
    def slot(*args, **kwargs):
        yield

    def process(argv, *, cwd, timeout_s, log_path, **kwargs):
        words = shlex.split(argv[-1])
        if "--collect-only" in words:
            return 5, False, ""
        report = next((word.split("=", 1)[1] for word in words if word.startswith("--junitxml=")), None)
        if report is not None:
            captured.append(words)
            assert words[words.index("-m") + 1] == "local_only"
            c.now[0] += timedelta(seconds=measured)
            suite = ET.Element("testsuite", time=str(measured))
            ET.SubElement(suite, "testcase", file=NODE.split("::")[0], name="test_machine", time=str(measured))
            ET.ElementTree(suite).write(report, encoding="unicode")
            evidence = next(word.split("=", 1)[1] for word in words if word.startswith("FA_REPORT="))
            Path(evidence).write_text(json.dumps({"complete": True, "exit_code": 0,
                                                "collected_ids": [NODE], "passing_ids": [NODE]}))
        elif NODE in words:
            protected.append(timeout_s)
            c.now[0] += timedelta(seconds=min(1000, timeout_s))
            return (143, True, "protected check timed out") if timeout_s < 1000 else (0, False, "")
        return 0, False, ""

    # Only external work is faked: process execution, slot acquisition and publication.
    # Real capture, selection, timing, verification and timeout retry paths run.
    monkeypatch.setattr(capacity, "hold_test_slot", slot)
    monkeypatch.setattr(runner, "run_logged", process)
    monkeypatch.setattr(run_baseline, "monotonic", c.ctx.monotonic)
    original = c.executor.run

    def implement(**kwargs):
        implemented.append(1)
        return original(**kwargs)

    def publish(attempt):
        published.append(1)
        c.tracker.remove_labels(1, ["running"])
        return "checked"

    monkeypatch.setattr(c.executor, "run", implement)
    monkeypatch.setattr(runner, "_publish", publish)
    outcome = runner.run_issue(1, c.ctx)
    assert outcome == "checked", c.store.log_path(1).read_text()
    assert len(captured) == len(implemented) == len(published) == 1
    assert protected == [expected], (
        f"CI protected selection must use its own {measured}s measurement: "
        f"expected one {expected}s allowance, got {protected}"
    )
