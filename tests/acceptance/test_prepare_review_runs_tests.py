"""Prepare must give an isolated reviewer executable checks and actual red evidence.

The toy project's runner needs a cache lock, just like uv/fa test. A fake Codex
seat enforces workspace-only writes and offline execution; no real agent or
network is used. Counts and output come from a real pytest run, never a canned
passing reviewer envelope. The runner also works when prepare supplies results
to the reviewer or provisions a usable environment inside its workspace.
"""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from foragent import capacity, executors, order_prepare, order_scratch
from foragent.config import Config
from foragent.executors import ExecResult
from foragent.git import GitRepo
from foragent.state import StateStore


TEST_PATH = "tests/acceptance/test_notice_delivery.py"
STATEMENT = "Sending a notice reports one delivery."
CANDIDATE = '''import json
import subprocess
import sys


def test_notice_delivery():
    result = subprocess.run([sys.executable, "notice.py", "send"],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["deliveries"] == 1
'''
MARKER = "PREPARE_CHECK_RESULT="

# This is a project check, not an agent decision: acquire its runtime lock, run
# pytest and report measured facts. An unavailable lock is a pre-collection error.
RUNNER = '''import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import uuid

facts = {"command": shlex.join([sys.executable, *sys.argv]), "collected": 0,
         "failed": 0, "exit_code": 2, "failure_kind": "environment",
         "run_id": uuid.uuid4().hex}
output = io.StringIO()
with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
    try:
        cache = Path(os.environ.get("UV_CACHE_DIR", Path.home() / ".cache" / "uv"))
        lock = cache / "prepare-check.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a"):
            import pytest

            class Counts:
                def pytest_collection_finish(self, session):
                    facts["collected"] = len(session.items)

                def pytest_runtest_logreport(self, report):
                    if report.when == "call" and report.failed:
                        facts["failed"] += 1

            facts["exit_code"] = int(pytest.main([
                "-q", "-s", "-o", "addopts=", "-p", "no:cacheprovider",
                "--rootdir=.", "--confcutdir=.", sys.argv[1],
            ], plugins=[Counts()]))
            if facts["collected"] and facts["failed"] and facts["exit_code"] == 1:
                facts["failure_kind"] = "assertion"
    except OSError as exc:
        print(f"{type(exc).__name__}: {exc}")
facts["output"] = output.getvalue()
directory = Path(".prepare-check")
directory.mkdir(exist_ok=True)
(directory / "result.json").write_text(json.dumps(facts))
print("PREPARE_CHECK_RESULT=" + json.dumps(facts))
sys.exit(facts["exit_code"])
'''

# A deterministic executor test double, independent of the operating system's
# sandbox implementation. The cache write really raises before pytest starts.
# Network audit events are refused before a connection or DNS lookup can happen.
SANDBOX = '''import os
from pathlib import Path
import runpy
import sys

root = Path.cwd().resolve()
def audit(event, args):
    path = None
    if event == "open" and not isinstance(args[0], int):
        if os.fspath(args[0]) == os.devnull:
            return
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            path = args[0]
    elif event in {"os.mkdir", "os.remove", "os.rmdir"}:
        path = args[0]
    if path is not None and not Path(path).resolve().is_relative_to(root):
        raise PermissionError(f"isolated reviewer cannot write outside workspace: {path}")
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise PermissionError("isolated reviewer has no network")

sys.addaudithook(audit)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def evidence_in(message):
    """Read the toy runner's structured output, including results supplied in a prompt."""
    for tail in message.split(MARKER)[1:]:
        try:
            value, _ = json.JSONDecoder().raw_decode(tail)
        except ValueError:
            continue
        if isinstance(value, dict) and "run_id" in value:
            return value
    return None


def run_check(command, tree, *, isolated):
    args = shlex.split(command)
    # The fake seat runs the exact project script/arguments under its permissions.
    argv = [args[0], "-c", SANDBOX, *args[1:]] if isolated else args
    result = subprocess.run(
        argv, cwd=tree, capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    facts = evidence_in(result.stdout)
    if facts is None:
        facts = {"command": command, "collected": 0, "failed": 0,
                 "exit_code": result.returncode, "output": result.stdout + result.stderr,
                 "failure_kind": "environment"}
    assert facts["exit_code"] == result.returncode, result
    return facts


@pytest.fixture
def review_case(git_history, tmp_path, monkeypatch):
    git_history.update({
        "notice.py": 'import json\nprint(json.dumps({"deliveries": 0}))\n',
        "check.py": RUNNER,
        ".gitignore": "__pycache__/\n.prepare-check/\n",
        "pytest.ini": "[pytest]\n",
    })
    root = git_history.clone
    config = Config(repo="owner/repo", language="en", executor="codex",
                    executor_model="writer-model", reviewer="codex",
                    reviewer_model="reviewer-model", skill_libraries=[])
    plan = {"id": "prepare-runtime", "title": "Notice delivery", "summary": STATEMENT,
            "spec_markdown": STATEMENT, "issues": [{
                "key": "notice", "title": "Deliver a notice", "goal": STATEMENT,
                "statements": [STATEMENT], "out_of_scope": "Delivery providers.",
                "done_when": ["true"], "lane": "main", "size": "s", "blocked_by": [],
            }]}
    case = SimpleNamespace(
        root=root, git=GitRepo(root), config=config, plan=plan,
        store=StateStore(tmp_path / "state"), calls=[], observed=[],
        path=root / "PLAN.json", base=git_history.tip,
        command=shlex.join([sys.executable, "check.py", TEST_PATH]),
    )
    case.path.write_text(json.dumps(plan))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "host-cache"))
    monkeypatch.chdir(root)
    # Admission is unrelated to making a sandbox's test runtime usable.
    monkeypatch.setattr(capacity, "hold_workload", lambda *a, **kw: nullcontext())

    def run(self, **kw):
        tree = Path(kw["worktree"])
        role = "reviewer" if kw["model"] == "reviewer-model" else "writer"
        case.calls.append({"role": role, **kw})
        if role == "writer":
            target = tree / TEST_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(CANDIDATE)
            message = {"acceptance_files": {TEST_PATH: CANDIDATE},
                       "done_when": [case.command], "questions": []}
        else:
            # Accept actual evidence delivered by prepare as well as a prepared
            # in-seat runtime, without requiring a broker/helper/API by name.
            facts = evidence_in(kw["prompt"])
            supplied = tree / ".prepare-check/result.json"
            if facts is None and supplied.is_file():
                facts = json.loads(supplied.read_text())
            if facts is None and case.command in kw["prompt"]:
                facts = run_check(case.command, tree, isolated=kw.get("isolated", False))
            if facts is None:
                facts = {"command": case.command, "collected": 0, "failed": 0,
                         "exit_code": 2, "output": "No command or execution evidence reached the reviewer.",
                         "failure_kind": "environment"}
            case.observed.append(facts)
            red = {key: facts[key] for key in (
                "command", "collected", "failed", "exit_code", "output", "failure_kind")}
            valid = facts["collected"] > 0 and facts["failed"] > 0 and facts["failure_kind"] == "assertion"
            message = {"verdict": "pass" if valid else "hold",
                       "reason": "Real notice assertion failure." if valid else "Environment failure: " + facts["output"],
                       "questions": [], "coverage": [{"statement": STATEMENT, "tests": [TEST_PATH],
                           "break_it": ["wrong count, skipped send or doubled send fails the delivery count"]}],
                       "red": [red]}
        log = Path(kw["log_path"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(message))
        return ExecResult(0, False, json.dumps(message), log, session_id=f"{role}-{len(case.calls)}")

    monkeypatch.setattr(executors.CodexExecutor, "run", run)
    return case


def prepare(case):
    rows = order_prepare.prepare(case.plan, case.root, case.config, case.git, case.store,
                                 plan_path=case.path)
    assert len(rows) == 1
    # Read through prepare's public status entry point too: the evidence must
    # survive recording, rather than only existing in the fake reviewer's reply.
    recorded = order_prepare.prepare(case.plan, case.root, case.config, case.git, case.store, status=True)
    assert recorded == rows
    return rows[0]


def test_prepare_records_collected_assertion_failure_for_the_isolated_reviewer(review_case, tmp_path, monkeypatch):
    case = review_case
    # Positive control: identical base bytes, candidate and command really collect
    # and fail when the runner has its normal runtime access. This rules out a
    # broken candidate/command masquerading as the missing prepare behaviour.
    control = tmp_path / "control"
    order_scratch.build(case.git, order_scratch.base_revision(case.git, "main"), control)
    target = control / TEST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(CANDIDATE)
    actual = run_check(case.command, control, isolated=False)
    assert (actual["collected"], actual["failed"], actual["exit_code"]) == (1, 1, 1), actual
    assert "assert 0 == 1" in actual["output"]

    # The fake sandbox can collect too when its runtime is usable. This is a
    # control of the fixture, not a requirement to fix prepare by moving a cache.
    with monkeypatch.context() as runtime:
        runtime.setenv("UV_CACHE_DIR", str(control / ".prepare-check/cache"))
        inside = run_check(case.command, control, isolated=True)
    assert (inside["collected"], inside["failed"], inside["exit_code"]) == (1, 1, 1), inside

    row = prepare(case)
    assert row["base_revision"] == case.base
    assert len(case.observed) == 1, "The independent reviewer must receive execution evidence"
    assert len(row.get("red", [])) == 1, row
    red = row["red"][0]
    assert red["command"] == case.command
    assert red["collected"] > 0, (
        "prepare must make its Done-when command runnable despite reviewer isolation; "
        f"no tests were collected: {red}"
    )
    assert (red["collected"], red["failed"], red["exit_code"]) == (1, 1, 1), red
    assert red["failure_kind"] == "assertion" and "assert 0 == 1" in red["output"]
    assert red["output"] == case.observed[0]["output"]
    assert row["status"] == "reviewed", row
    assert json.loads(case.path.read_text())["issues"][0]["acceptance_files"] == {TEST_PATH: CANDIDATE}


def test_prepare_keeps_the_reviewer_workspace_only_and_offline(review_case):
    case = review_case
    prepare(case)
    reviews = [call for call in case.calls if call["role"] == "reviewer"]
    assert len(reviews) == 1, case.calls
    review = reviews[0]
    assert review.get("isolated") is True
    assert not review.get("add_dirs"), "No extra writable directories may be granted"
    assert not review.get("resume_session_id"), "Review must be independent of the writer"
    argv = executors.codex_command(
        review["worktree"], review["log_path"], isolated=review["isolated"],
        add_dirs=review.get("add_dirs", ()), read_only=review.get("read_only", False),
    )
    assert "sandbox_workspace_write.writable_roots=[]" in argv
    assert "sandbox_workspace_write.network_access=false" in argv
    assert argv[argv.index("-s") + 1] in {"workspace-write", "read-only"}
    assert "--add-dir" not in argv
    assert {"--ignore-user-config", "--ignore-rules", "notify=[]", "mcp_servers={}"} <= set(argv)
    assert argv[argv.index("--disable") + 1] == "hooks"


def test_prepare_reports_a_wrong_command_as_environment_failure(review_case):
    case = review_case
    case.command = shlex.join([sys.executable, "missing_check_runner.py", TEST_PATH])
    row = prepare(case)
    assert row["status"] == "hold", row
    assert len(case.observed) == 1 and len(row.get("red", [])) == 1, row
    evidence = row["red"][0]
    assert evidence["command"] == case.command
    assert evidence["collected"] == 0 and evidence["failed"] == 0
    assert evidence["exit_code"] != 0 and evidence["failure_kind"] == "environment"
    assert "missing_check_runner.py" in evidence["output"]
    assert "Environment failure" in row["reason"]
    assert "acceptance_files" not in json.loads(case.path.read_text())["issues"][0]
