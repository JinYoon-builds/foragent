"""Execute generated steps against trusted tools from before cache support."""

import json
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from foragent.ci_workflow import workflow_text
from foragent.config import Config


@pytest.fixture
def workflow_shell(tmp_path, monkeypatch):
    jobs = yaml.safe_load(workflow_text(Config()))["jobs"]
    trusted = tmp_path / "trusted"
    package = trusted / "foragent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    uv = binaries / "uv"
    # Keep the generated shell command, replacing only network installation.
    uv.write_text("#!/bin/sh\nwhile [ \"$1\" != python ]; do shift; done\nshift\n"
                  f"exec {shlex.quote(sys.executable)} \"$@\"\n")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONPATH", str(trusted))
    monkeypatch.delenv("FA_BASE_CACHE_MODE", raising=False)
    cache = tmp_path / "restored base.json"
    cache.write_text("stale or unverified record")
    output, log, report = (tmp_path / name for name in ("outputs", "executed", "report.json"))
    monkeypatch.setenv("FA_BASE_CACHE_FILE", str(cache))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("FA_REPORT_FILE", str(report))
    monkeypatch.setenv("EXECUTION_LOG", str(log))

    def run(step):
        env = dict(os.environ)
        if "FA_BASE_CACHE_MODE" in step.get("env", {}):
            env["FA_BASE_CACHE_MODE"] = step["env"]["FA_BASE_CACHE_MODE"]
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                              cwd=tmp_path, env=env, capture_output=True, text=True)

    return SimpleNamespace(jobs=jobs, package=package, cache=cache, output=output, log=log,
                           report=report, run=run)


@pytest.mark.parametrize("verifier", ["missing", "broken"])
def test_pr_without_working_cache_module_runs_the_legacy_comparison(workflow_shell, verifier):
    case = workflow_shell
    if verifier == "broken":
        (case.package / "ci_base_cache.py").write_text("raise RuntimeError('verification unavailable')\n")
    (case.package / "ci_shard.py").write_text(
        "import json, os\nfrom pathlib import Path\n"
        "assert not Path(os.environ['FA_BASE_CACHE_FILE']).exists(), 'unverified cache reached comparison'\n"
        "assert 'GH_TOKEN' not in os.environ\n"
        "Path(os.environ['EXECUTION_LOG']).write_text('base\\nhead\\n')\n"
        "Path(os.environ['FA_REPORT_FILE']).write_text(json.dumps({'base': ['passing-node'], 'head': []}))\n"
        "Path(os.environ['GITHUB_OUTPUT']).write_text('comparison=true\\n')\n")
    steps = case.jobs["acceptance"]["steps"]
    verify = next(step for step in steps if "foragent.ci_base_cache" in step.get("run", ""))
    compared = next(step for step in steps if step.get("id") == "compare")
    result = case.run(verify)
    assert result.returncode == 0, result.stderr
    if verifier == "missing":
        assert "No module named foragent.ci_base_cache" in result.stderr
    assert not case.cache.exists()
    result = case.run(compared)
    assert result.returncode == 0, result.stderr
    assert case.log.read_text() == "base\nhead\n"
    assert json.loads(case.report.read_text()) == {"base": ["passing-node"], "head": []}
    assert case.output.read_text() == "comparison=true\n"
    assert not any(step.get("uses", "").startswith("actions/cache/save@") for step in steps)


def test_available_verifier_keeps_its_verified_record(workflow_shell):
    case = workflow_shell
    (case.package / "ci_base_cache.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "with Path(os.environ['EXECUTION_LOG']).open('a') as log: log.write('verify\\n')\n")
    original = case.cache.read_bytes()
    verify = next(step for step in case.jobs["acceptance"]["steps"]
                  if "foragent.ci_base_cache" in step.get("run", ""))
    result = case.run(verify)
    assert result.returncode == 0, result.stderr
    assert case.cache.read_bytes() == original
    assert case.log.read_text() == "verify\n"


@pytest.mark.parametrize("producer", ["missing", "supported", "disabled", "failed"])
def test_producer_only_saves_a_fresh_result_from_a_capable_tool(workflow_shell, producer):
    case = workflow_shell
    code = "import os\nfrom pathlib import Path\n"
    if producer != "missing":
        code += "def produce(*args): pass\n"
    code += ("if __name__ == '__main__':\n"
             "    with Path(os.environ['EXECUTION_LOG']).open('a') as log: log.write('produce\\n')\n")
    if producer in {"supported", "failed"}:
        code += "    Path(os.environ['FA_BASE_CACHE_FILE']).write_text('fresh producer result')\n"
    if producer == "failed":
        code += "    raise RuntimeError('incomplete measurement')\n"
    (case.package / "ci_shard.py").write_text(code)
    steps = case.jobs["acceptance-base"]["steps"]
    produce = next(step for step in steps if step.get("env", {}).get("FA_BASE_CACHE_MODE") == "produce")
    save = next(step for step in steps if step.get("uses", "").startswith("actions/cache/save@"))
    result = case.run(produce)
    assert (result.returncode == 0) is (producer != "failed"), result.stderr
    if producer == "missing":
        assert not case.log.exists(), "Do not execute a legacy comparison as a producer"
    else:
        assert case.log.read_text() == "produce\n"
    if producer in {"missing", "disabled"}:
        assert not case.cache.exists(), "A skipped producer must not save a stale record"
    else:
        assert case.cache.read_text() == "fresh producer result"
    outputs = dict(line.split("=", 1) for line in case.output.read_text().splitlines()) if case.output.exists() else {}
    assert (outputs.get("cache") == "true") is (producer == "supported")
    assert save.get("if") == f"steps.{produce['id']}.outputs.cache == 'true'"
