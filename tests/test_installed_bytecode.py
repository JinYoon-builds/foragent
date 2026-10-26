"""Gate start environments really keep a fresh import from writing bytecode."""

import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import foragent
from foragent import launchd, launcher


def _fresh_import(tmp_path, environment):
    """Import a fresh copy of the package in a real child; return the bytecode it wrote."""
    site = tmp_path / "site-packages"
    shutil.copytree(Path(foragent.__file__).parent, site / "foragent",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    child = {key: value for key, value in environment.items() if key != "PYTHONPATH"}
    child["PYTHONPATH"] = str(site)
    subprocess.run([sys.executable, "-c", "import foragent.cli, foragent.launchd; assert foragent.__file__.startswith(%r)"
                    % str(site)], env=child, cwd=tmp_path, check=True, capture_output=True, timeout=120)
    return sorted(path.relative_to(site) for path in site.rglob("*.pyc"))


def _without_flag():
    return {key: value for key, value in os.environ.items() if key != "PYTHONDONTWRITEBYTECODE"}


def test_control_a_plain_fresh_import_writes_package_bytecode(tmp_path, monkeypatch):
    assert _fresh_import(tmp_path, _without_flag()), "the check must be able to see bytecode"


def test_spawned_gate_processes_write_no_bytecode(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", _without_flag())
    assert _fresh_import(tmp_path, launcher.gate_environment()) == []


def test_plist_environment_writes_no_bytecode(tmp_path):
    job = launchd.Launchd(tmp_path, repo="o/r", home=tmp_path, state_root=tmp_path / "state",
                          executable=tmp_path / "fa")
    environment = plistlib.loads(job.plist())["EnvironmentVariables"]
    assert _fresh_import(tmp_path, {**_without_flag(), **environment}) == []
