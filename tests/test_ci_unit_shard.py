"""Unit shards optionally preserve pytest's per-test JUnit results."""

import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from foragent import ci_unit_shard


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

    def run(command, *, check):
        calls.append((command, check))
        return subprocess.CompletedProcess(command, exit_code)

    monkeypatch.setattr(ci_unit_shard.subprocess, "run", run)

    assert ci_unit_shard.main() == exit_code
    assert len(calls) == 1
    command, check = calls[0]
    assert check is False
    report_args = [arg for arg in command if arg.startswith("--junitxml=")]
    assert report_args == ([f"--junitxml={report_file}"] if report_file else [])
    assert [arg for arg in command if arg not in report_args] == [
        sys.executable, "-m", "pytest", "-q", "-m", "not local_only",
        "tests/test_bravo.py", "tests/test_delta.py",
    ]


def test_ci_unit_shard_junit_records_results_even_when_a_test_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FA_SHARD", "0")
    monkeypatch.setenv("FA_SHARDS", "1")
    report = tmp_path / "reports/unit 0.xml"
    monkeypatch.setenv("FA_REPORT_FILE", str(report))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:cacheprovider")
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers = local_only: requires machine tools\n", encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").write_text(
        "import pytest\n"
        "def test_passed():\n    pass\n"
        "@pytest.mark.skip(reason='unavailable on this platform')\n"
        "def test_skipped():\n    pass\n"
        "def test_failed():\n    assert False\n"
        "@pytest.mark.local_only\n"
        "def test_local():\n    assert False\n",
        encoding="utf-8",
    )

    assert ci_unit_shard.main() == 1

    cases = {case.attrib["name"]: case for case in ET.parse(report).iter("testcase")}
    assert set(cases) == {"test_passed", "test_skipped", "test_failed"}
    assert len(cases["test_passed"]) == 0
    assert cases["test_skipped"].find("skipped") is not None
    assert cases["test_failed"].find("failure") is not None
