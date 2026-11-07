"""Test preparation's sandboxes give real-terminal tests pseudo-terminal devices (prepare-pty).

No real agent runs. A fake Claude seat stands in for Claude Code's sandbox and enforces the
one capability under test from the settings file prepare hands it: without the documented
``sandbox.allowPty`` switch (Claude Code: "Allow pseudo-terminal (pty) operations") every
pseudo-terminal call in the seat fails with ``OSError('out of pty devices')``, exactly as the
real preparation sandbox does on main (r982). With the switch the seat runs the test as is.

The candidate real-terminal test opens a real pseudo-terminal and fails by assertion, so a
seat that can open ptys reports an assertion failure and a seat that cannot reports the OSError.
No process is forked and no signal is sent.
"""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from foragent import capacity, executors, order_prepare, order_prepare_check, order_scratch
from foragent.config import Config
from foragent.executors import ExecResult
from foragent.git import GitRepo
from foragent.state import StateStore


TEST_PATH = "tests/acceptance/test_terminal_greeting.py"
NODE = TEST_PATH + "::test_terminal_shows_the_greeting"
STATEMENT = "The terminal shows the greeting."
CANDIDATE = '''import os
import pty


def test_terminal_shows_the_greeting():
    master, slave = pty.openpty()
    try:
        assert os.isatty(slave)
        os.write(slave, b"hello\\n")
        echoed = os.read(master, 1024)
    finally:
        os.close(slave)
        os.close(master)
    assert b"WELCOME" in echoed
'''

# The fake seat: refuse every pseudo-terminal call unless the seat allows ptys, then run the
# exact command. Mirrors the real failure text of the preparation sandbox on main.
SEAT = '''import os
import pty
import runpy
import sys

allowed = sys.argv.pop(1) == "allow"
if not allowed:
    def refuse(*args, **kwargs):
        raise OSError("out of pty devices")
    os.openpty = refuse
    if hasattr(os, "forkpty"):
        os.forkpty = refuse
    pty.openpty = refuse
    pty.fork = refuse
sys.argv = sys.argv[1:]
if sys.argv[0] == "-m":
    module = sys.argv.pop(1)
    sys.argv[0] = module
    runpy.run_module(module, run_name="__main__", alter_sys=True)
else:
    runpy.run_path(sys.argv[0], run_name="__main__")
'''


def pty_allowed(kw) -> bool:
    """What Claude Code's sandbox would allow, read from the settings prepare passed the seat."""
    settings_path = kw.get("settings_path")
    assert settings_path is not None, "a Claude preparation seat always gets its sandbox settings"
    sandbox = json.loads(Path(settings_path).read_text())["sandbox"]
    # The seat must stay confined while ptys open: these are the seat's other limits.
    assert sandbox["enabled"] is True and sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["network"]["allowedDomains"] == []
    return sandbox.get("allowPty") is True


def seat_run(tree: Path, report: Path, *, allowed: bool) -> dict:
    """Run the candidate inside the fake seat and read pytest's own report of the failure."""
    argv = [sys.executable, "-c", SEAT, "allow" if allowed else "deny", "-m", "pytest", "-q",
            "-p", "no:cacheprovider", "-p", "no:xdist", "-o", "addopts=", "--rootdir=.",
            "--confcutdir=" + str(tree / "tests/acceptance"), f"--junitxml={report}", TEST_PATH]
    result = subprocess.run(argv, cwd=tree, capture_output=True, text=True,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                 "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    facts = {"exit_code": result.returncode, "output": result.stdout + result.stderr,
             "collected": 0, "failure_kind": "environment"}
    if report.is_file():
        suite = ET.parse(report).getroot()
        suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
        facts["collected"] = int(suite.get("tests", 0))
        failure = suite.find("testcase/failure")
        error = suite.find("testcase/error")
        if failure is not None and error is None:
            text = (failure.get("message") or "") + (failure.text or "")
            facts["failure_kind"] = ("assertion" if (failure.get("message") or "").startswith("AssertionError")
                                     else "error")
            facts["failure"] = text
    report.unlink(missing_ok=True)
    return facts


@pytest.fixture
def pty_case(git_history, tmp_path, monkeypatch):
    git_history.update({".gitignore": "__pycache__/\n", "pytest.ini": "[pytest]\n"})
    root = git_history.clone
    config = Config(repo="owner/repo", language="en", executor="claude",
                    executor_model="writer-model", reviewer="claude",
                    reviewer_model="reviewer-model", skill_libraries=[])
    plan = {"id": "prepare-terminal", "title": "Terminal greeting", "summary": STATEMENT,
            "spec_markdown": STATEMENT, "issues": [{
                "key": "greeting", "title": "Show the greeting", "goal": STATEMENT,
                "statements": [STATEMENT], "out_of_scope": "Colours.",
                "done_when": ["true"], "lane": "main", "size": "s", "blocked_by": [],
            }]}
    command = shlex.join([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          "-p", "no:xdist", "-o", "addopts=", TEST_PATH])
    case = SimpleNamespace(root=root, git=GitRepo(root), config=config, plan=plan,
                           store=StateStore(tmp_path / "state"), path=root / "PLAN.json",
                           base=git_history.tip, command=command, calls=[], seats=[],
                           reports=tmp_path / "seat-reports")
    case.reports.mkdir()
    case.path.write_text(json.dumps(plan))
    monkeypatch.chdir(root)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setattr(capacity, "hold_workload", lambda *a, **kw: nullcontext())

    def run(self, **kw):
        tree = Path(kw["worktree"])
        role = "reviewer" if kw["model"] == "reviewer-model" else "writer"
        case.calls.append({"role": role, **kw})
        if role == "writer":
            target = tree / TEST_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(CANDIDATE)
        allowed = pty_allowed(kw)
        facts = seat_run(tree, case.reports / f"{role}-{len(case.calls)}.xml", allowed=allowed)
        case.seats.append({"role": role, "allowed": allowed, **facts})
        if role == "writer":
            message = {"acceptance_files": [TEST_PATH], "done_when": [case.command],
                       "questions": [], "notes": []}
        else:
            valid = facts["collected"] == 1 and facts["exit_code"] == 1 and facts["failure_kind"] == "assertion"
            red = {"command": case.command, "collected": facts["collected"],
                   "failed": 1 if facts["exit_code"] == 1 else 0, "exit_code": facts["exit_code"],
                   "output": facts["output"][-2000:], "failure_kind": facts["failure_kind"]}
            message = {"verdict": "pass" if valid else "hold",
                       "reason": ("The greeting assertion fails on main." if valid
                                  else "The test cannot run here: " + facts["output"][-500:]),
                       "questions": [], "notes": [],
                       "coverage": [{"statement": STATEMENT, "tests": [NODE],
                                     "break_it": ["a missing, wrong or doubled greeting fails the check"]}],
                       "red": [red]}
        log = Path(kw["log_path"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(message))
        return ExecResult(0, False, json.dumps(message), log, session_id=f"{role}-{len(case.calls)}")

    monkeypatch.setattr(executors.ClaudeExecutor, "run", run)
    return case


def prepare(case):
    rows = order_prepare.prepare(case.plan, case.root, case.config, case.git, case.store,
                                 plan_path=case.path)
    assert len(rows) == 1, rows
    return rows[0]


def control(case, tmp_path):
    """The candidate itself is a sound real-terminal test: on this host it fails by assertion."""
    copy = tmp_path / "control"
    order_scratch.build(case.git, order_scratch.base_revision(case.git, "main"), copy)
    (copy / TEST_PATH).parent.mkdir(parents=True, exist_ok=True)
    (copy / TEST_PATH).write_text(CANDIDATE)
    allowed = seat_run(copy, tmp_path / "control-allowed.xml", allowed=True)
    assert allowed["failure_kind"] == "assertion", (
        "the host running this check must have pseudo-terminals: " + allowed["output"][-1000:])
    refused = seat_run(copy, tmp_path / "control-refused.xml", allowed=False)
    assert "out of pty devices" in refused["output"] and refused["failure_kind"] != "assertion"


def test_a_pty_test_runs_in_the_writer_and_reviewer_sandboxes(pty_case, tmp_path):
    case = pty_case
    control(case, tmp_path)
    prepare(case)
    roles = [seat["role"] for seat in case.seats]
    assert roles.count("writer") >= 1 and roles.count("reviewer") >= 1, case.seats
    for seat in case.seats:
        assert seat["allowed"], f"the {seat['role']} sandbox gives the test no pseudo-terminals"
        assert "out of pty devices" not in seat["output"], seat["output"][-1000:]
        assert (seat["collected"], seat["exit_code"], seat["failure_kind"]) == (1, 1, "assertion"), seat


def test_a_pty_test_failing_by_assertion_is_red_evidence_for_prepare_and_publication(
        pty_case, tmp_path, monkeypatch):
    case = pty_case
    control(case, tmp_path)
    row = prepare(case)
    assert row["status"] == "reviewed", row
    assert row.get("red") and row["red"][0]["failure_kind"] == "assertion", row
    assert "out of pty devices" not in row["red"][0]["output"]
    assert json.loads(case.path.read_text())["issues"][0].get("acceptance_files"), \
        "the reviewed pty test is recorded as the issue's acceptance test"

    # The publication red check measures the same command with structured pytest facts.
    original = order_scratch.build

    def build(git, base, tree, context):
        original(case.git, base, tree)
        (tree / TEST_PATH).parent.mkdir(parents=True, exist_ok=True)
        (tree / TEST_PATH).write_text(CANDIDATE)
        return order_scratch._entries(tree)

    monkeypatch.setattr(order_scratch, "build", build)
    directory = tmp_path / "publication"
    directory.mkdir()
    [measured] = order_prepare_check.run(case.git, case.base, None, lambda tree: None,
                                         lambda *args: [], [case.command], directory, 120,
                                         collect_node_ids=True)
    assert measured["exit_code"] == 1, measured["output"][-1000:]
    assert measured["failing_node_ids"] == [NODE] == measured["collected_node_ids"]
    assert measured["nonred_node_ids"] == []
    assert "out of pty devices" not in measured["output"]
