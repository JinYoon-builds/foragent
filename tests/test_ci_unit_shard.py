"""Unit shards optionally preserve pytest's per-test JUnit results."""

import subprocess
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
import yaml

from foragent import ci_unit_shard
from foragent.run_checks import junit_node_id


@pytest.mark.parametrize("report_file", [None, "", "reports/unit 1.xml"],
                         ids=["unset", "empty", "report-with-spaces"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_ci_unit_shard_command_adds_only_requested_report(
    tmp_path, monkeypatch, report_file, exit_code,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FA_SHARD", "1")
    monkeypatch.setenv("FA_SHARDS", "2")
    if report_file is None:
        monkeypatch.delenv("FA_REPORT_FILE", raising=False)
    else:
        monkeypatch.setenv("FA_REPORT_FILE", report_file)
    (tmp_path / "tests").mkdir()
    for name in ("alpha", "bravo", "charlie", "delta"):
        (tmp_path / f"tests/test_{name}.py").touch()
    calls = []
    git_calls = []

    def git_head(command, *, text):
        git_calls.append(command)
        assert text is True
        return "a" * 40 + "\n"

    def run(command, *, check):
        calls.append((command, check))
        if report_file:
            assert not Path(report_file).exists()
            Path(report_file).parent.mkdir(parents=True, exist_ok=True)
            Path(report_file).write_text('<testsuites><testsuite/></testsuites>')
        return subprocess.CompletedProcess(command, exit_code)

    if report_file:
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text('stale report')
    monkeypatch.setattr(ci_unit_shard.subprocess, "check_output", git_head)
    monkeypatch.setattr(ci_unit_shard.subprocess, "run", run)

    assert ci_unit_shard.main() == exit_code
    assert len(calls) == 1
    command, check = calls[0]
    assert check is False
    assert command == [
        sys.executable, "-m", "pytest", "-q", "-m", "not local_only",
        "tests/test_bravo.py", "tests/test_delta.py",
    ] + ([f"--junitxml={report_file}", "-o", "junit_family=legacy", "--junit-prefix="] if report_file else [])
    assert git_calls == ([["git", "rev-parse", "HEAD"]] if report_file else [])
    if report_file:
        assert ET.parse(report_file).find('.//property').attrib == {
            'name': 'foragent_tested_commit', 'value': 'a' * 40}


def test_ci_unit_shard_junit_records_results_even_when_a_test_fails(git_history, tmp_path, monkeypatch):
    root = git_history.clone
    monkeypatch.chdir(root)
    monkeypatch.setenv("FA_SHARD", "0")
    monkeypatch.setenv("FA_SHARDS", "1")
    report = tmp_path / "reports/unit 0.xml"
    monkeypatch.setenv("FA_REPORT_FILE", str(report))
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)  # Actions may identify the synthetic merge.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:cacheprovider --junit-prefix=stale")
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    (root / "pytest.ini").write_text(
        "[pytest]\nmarkers = local_only: requires machine tools\n", encoding="utf-8",
    )
    (root / "tests/test_sample.py").write_text(
        "import pytest\n"
        "def test_passed():\n    pass\n"
        "@pytest.mark.skip(reason='unavailable on this platform')\n"
        "def test_skipped():\n    pass\n"
        "def test_failed():\n    assert False\n"
        "@pytest.mark.local_only\n"
        "def test_local():\n    assert False\n"
        "class TestOuter:\n"
        "    class TestInner:\n"
        "        @pytest.mark.parametrize('value', [1], ids=['with.dot'])\n"
        "        def test_value(self, value):\n            assert value == 1\n",
        encoding="utf-8",
    )

    assert ci_unit_shard.main() == 1

    cases = {case.attrib["name"]: case for case in ET.parse(report).iter("testcase")}
    assert {junit_node_id(case) for case in cases.values()} == {
        "tests/test_sample.py::test_passed", "tests/test_sample.py::test_skipped",
        "tests/test_sample.py::test_failed", "tests/test_sample.py::TestOuter::TestInner::test_value[with.dot]",
    }
    assert len(cases["test_passed"]) == 0
    assert cases["test_skipped"].find("skipped") is not None
    assert cases["test_failed"].find("failure") is not None
    assert ET.parse(report).find('.//property[@name="foragent_tested_commit"]').get("value") == git_history.tip


def test_unit_workflow_tests_the_pr_head_or_push_commit():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
    job = yaml.safe_load(workflow.read_text())["jobs"]["test"]
    checkout, = [step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")]
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha || github.sha }}"
