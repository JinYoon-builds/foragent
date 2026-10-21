"""Acceptance: `fa adopt baseline --json` runs existing tests once, without the acceptance dir."""

import json
from pathlib import Path
import sys

from foragent import cli

PYTEST = f"{sys.executable} -m pytest"


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def baseline(root: Path, capsys, *extra: str) -> dict:
    assert cli.main(["adopt", "baseline", "--repo-root", str(root), "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def test_reports_already_broken_tests_but_not_acceptance_tests(tmp_path, capsys):
    write(tmp_path, {
        "tests/test_ok.py": "def test_fine():\n    assert True\n",
        "tests/test_bad.py": "def test_broken():\n    assert False\n",
        "tests/acceptance/test_locked.py": "def test_not_built_yet():\n    assert False\n",
    })
    report = baseline(tmp_path, capsys, "--command", PYTEST)
    assert report["passed"] is False
    assert report["exit_code"] != 0
    assert report["failing"] == ["tests/test_bad.py::test_broken"]
    assert "--ignore=tests/acceptance" in report["command"]


def test_green_baseline(tmp_path, capsys):
    write(tmp_path, {"tests/test_ok.py": "def test_fine():\n    assert True\n"})
    report = baseline(tmp_path, capsys, "--command", PYTEST)
    assert report["passed"] is True
    assert report["exit_code"] == 0
    assert report["failing"] == []


def test_custom_acceptance_dir_from_config(tmp_path, capsys):
    write(tmp_path, {
        ".foragent.yml": "acceptance_dir: checks/locked/\n",
        "tests/test_ok.py": "def test_fine():\n    assert True\n",
        "checks/locked/test_locked.py": "def test_not_built_yet():\n    assert False\n",
    })
    report = baseline(tmp_path, capsys, "--command", PYTEST)
    assert report["passed"] is True
    assert "--ignore=checks/locked" in report["command"]


def test_no_test_command_is_skipped_not_failed(tmp_path, capsys):
    write(tmp_path, {"notes.txt": "nothing to run\n"})
    report = baseline(tmp_path, capsys)
    assert report["command"] is None
    assert report["passed"] is None
    assert "no test command" in report["skipped"]
