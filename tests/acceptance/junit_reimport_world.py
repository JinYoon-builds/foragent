"""Helper for junit-node-id acceptance tests: a tiny project where one test
module imports a test function from another, run under real pytest."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

DEFINING = "tests/acceptance/test_defining.py"
COLLECTING = "tests/acceptance/test_collecting.py"

DEFINING_SOURCE = '''\
import pytest


@pytest.mark.parametrize("mode", ["parallel"])
def test_shared_budget(mode):
    assert mode == "parallel"


def test_plain():
    assert True


class TestGroup:
    def test_inside(self):
        assert True
'''

COLLECTING_SOURCE = '''\
from test_defining import test_shared_budget  # noqa: F401  (re-collected here)


def test_guard_own():
    assert True
'''

# The id the defining module's own copy has today; it must stay exactly so.
DEFINING_ID = f"{DEFINING}::test_shared_budget[parallel]"
# The id that selects the collecting module's copy.
COLLECTING_ID = f"{COLLECTING}::test_shared_budget[parallel]"


def make_project(root: Path) -> Path:
    (root / "pytest.ini").write_text("[pytest]\n")
    folder = root / "tests" / "acceptance"
    folder.mkdir(parents=True)
    (root / DEFINING).write_text(DEFINING_SOURCE)
    (root / COLLECTING).write_text(COLLECTING_SOURCE)
    return root


def _env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def pytest_run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--rootdir=.", *args],
        cwd=root, env=_env(), capture_output=True, text=True, timeout=120,
    )


def junit_report(root: Path, *selection: str, name: str = "report.xml") -> Path:
    """Run the suite the way Foragent's baseline does and return the JUnit file."""
    report = root / name
    result = pytest_run(root, "-q", *(selection or ("tests/acceptance",)),
                        "-o", "junit_family=legacy", "--junit-prefix=", f"--junitxml={report}")
    assert result.returncode == 0, result.stdout + result.stderr
    return report


def cases(report: Path) -> list[ET.Element]:
    return list(ET.parse(report).iter("testcase"))


def rerun(root: Path, node_id: str, name: str) -> tuple[subprocess.CompletedProcess[str], list[ET.Element]]:
    """Select exactly one id with real pytest; return the run and its testcases."""
    report = root / name
    result = pytest_run(root, "-q", node_id, "-o", "junit_family=legacy", "--junit-prefix=",
                        f"--junitxml={report}")
    found = cases(report) if report.exists() else []
    return result, found
