"""Acceptance test for doctor-live. Locked by Foragent; do not edit."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest
import yaml

from conftest import ACCEPTANCE, git_command
from test_local_truth import record_gh
from test_skills_autosync import cli_exit, isolated_home


FAKE_EXECUTOR = r'''
import json
import os
from pathlib import Path
import sys
import time

name, argv = sys.argv[1], sys.argv[2:]
stdin = sys.stdin.read()
worktree = Path(argv[argv.index("-C") + 1]) if "-C" in argv else Path.cwd()
messages = list(argv)
stream_json = "--input-format=stream-json" in argv or (
    "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
)
if stream_json:
    for line in stdin.splitlines():
        event = json.loads(line)
        content = event.get("message", event).get("content", "")
        if isinstance(content, list):
            content = "\n".join(part["text"] for part in content if part.get("type") == "text")
        messages.append(content)
else:
    messages.append(stdin)
conditions = [message.lstrip()[6:] for message in messages if message.lstrip().startswith("/goal ")]
references = "\n".join(messages)
automatic = {worktree / item for item in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")}
files = automatic | set(worktree.rglob("*"))
files |= set(Path(os.environ["TMPDIR"]).rglob("*"))
context = []
for path in files:
    relative = path.relative_to(worktree).as_posix() if path.is_relative_to(worktree) else None
    if path not in automatic and str(path) not in references and not (relative and relative in references):
        continue
    try:
        if path.is_file():
            context.append(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        pass
delivered = references + "\n" + "\n".join(context)
mode = os.environ.get("FA_DOCTOR_LIVE_MODE", "success")
error = None
if name == "claude" and mode == "strict" and any(len(text) > 4000 for text in conditions):
    size = max(map(len, conditions))
    error = f"Goal condition is limited to 4000 characters (got {size})"
elif "hello.txt" not in delivered:
    error = "No hello.txt smoke task reached the executor"
elif mode != "no-artifact":
    (worktree / "hello.txt").write_text("ok\n", encoding="utf-8")

session = "doctor-live-" + name + "-session"
if name == "claude":
    events = [{"type": "system", "subtype": "init", "session_id": session}]
    if error:
        events += [{"type": "assistant", "message": {
            "role": "assistant", "model": "<synthetic>",
            "content": [{"type": "text", "text": error}],
        }}]
    events += [{"type": "result", "subtype": "success", "is_error": False,
                "session_id": session, "num_turns": 0 if error else 3,
                "total_cost_usd": 0, "result": error or "Created hello.txt containing ok"}]
else:
    events = [{"type": "thread.started", "thread_id": session}]
    events += ([{"type": "error", "message": error}] if error else
               [{"type": "turn.started"}, {"type": "turn.completed"}] * 3)
    if "-o" in argv:
        Path(argv[argv.index("-o") + 1]).write_text(error or "Created hello.txt containing ok")

seed = worktree / "tests/acceptance/test_feature.py"
row = {"executor": name, "argv": argv, "cwd": str(worktree.resolve()), "stdin": stdin,
       "delivered": delivered, "goal_lengths": list(map(len, conditions)), "error": error,
       "seed": seed.read_text() if seed.is_file() else None,
       "created": (worktree / "hello.txt").is_file() and
                  (worktree / "hello.txt").read_text() == "ok\n",
       "git_checkout": (worktree / ".git").exists()}
with Path(os.environ["FA_DOCTOR_LIVE_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(row) + "\n")
time.sleep(0.02)
for event in events:
    print(json.dumps(event), flush=True)
# A synthetic refusal can have exit 0 and is_error=false. Doctor must inspect
# the real executor result, rather than treating a successful spawn as PASS.
'''


@pytest.fixture
def live_doctor(git_history, isolated_home, tmp_path, monkeypatch):
    """Real local processes; no fake executor objects or run_logged replacement."""
    root = git_history.clone
    (root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "offline/doctor-fixture", "executor": "claude", "reviewer": "codex",
        "review": "fast", "skill_libraries": [], "test_command": "true",
    }))
    directory = tmp_path / "bin"
    directory.mkdir()
    # A closed PATH also makes missing-binary cases independent of installed CLIs.
    for name in ("git", "sh", "bash", "cat", "test", "printf"):
        executable = shutil.which(name)
        assert executable, f"The offline fixture needs {name}"
        (directory / name).symlink_to(executable)
    for name in ("python", "python3"):
        (directory / name).symlink_to(sys.executable)
    driver = directory / "executor.py"
    driver.write_text(FAKE_EXECUTOR, encoding="utf-8")
    for name in ("claude", "codex"):
        wrapper = directory / name
        wrapper.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(driver))} "
            f"{name} \"$@\"\n", encoding="utf-8",
        )
        wrapper.chmod(0o755)
    log = tmp_path / "executor-calls.jsonl"
    log.write_text("")
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.setenv("FA_DOCTOR_LIVE_LOG", str(log))
    monkeypatch.setenv("FA_DOCTOR_LIVE_MODE", "success")
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    gh = record_gh(monkeypatch)
    launches = []
    popen = subprocess.Popen

    def local_process(argv, *args, **kwargs):
        assert isinstance(argv, (list, tuple)) and argv, argv
        name = Path(argv[0]).name
        assert name in {"git", "sh", "bash", "claude", "codex", "python", "python3", Path(sys.executable).name}, argv
        if name == "git":
            assert not {"commit", "push", "reset", "checkout", "stash"}.intersection(argv), argv
        if name in {"claude", "codex"}:
            child_path = kwargs.get("env", os.environ).get("PATH", "")
            executable = shutil.which(str(argv[0]), path=child_path)
            assert executable is None or Path(executable).resolve() == (directory / name).resolve(), (
                f"Doctor attempted to bypass the offline {name} binary: {argv}"
            )
            frame, modules = sys._getframe(1), set()
            while frame is not None:
                modules.add(frame.f_globals.get("__name__"))
                frame = frame.f_back
            launches.append({"executor": name, "modules": modules})
        return popen(argv, *args, **kwargs)

    def no_network(*args, **kwargs):
        pytest.fail("doctor-live acceptance attempted a network connection")

    monkeypatch.setattr(subprocess, "Popen", local_process)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.chdir(root)
    return SimpleNamespace(root=root, bin=directory, gh=gh, launches=launches,
                           seed=(root / ACCEPTANCE).read_text(),
                           calls=lambda: [json.loads(line) for line in log.read_text().splitlines()])


def invoke_doctor(case, capsys, *options):
    from foragent.cli import main

    code = cli_exit(main, ["doctor", "--live", *options])
    captured = capsys.readouterr()
    return code, "\n".join(part.strip() for part in (captured.out, captured.err) if part.strip())


def assert_report(report, executor, verdict):
    lines = report.splitlines()
    assert len(lines) == 1, f"Expected one doctor result line; received:\n{report}"
    assert re.search(rf"\b{executor}\b", lines[0]), report
    assert re.search(rf"\b{verdict}\b", lines[0]), report
    assert "Traceback" not in report
    if verdict == "PASS":
        assert re.search(r"\b3\s+turns?\b|\bturns?\s*[:=]?\s*3\b", report), report
        assert re.search(r"\bseconds?\s*[:=]\s*\d+(?:\.\d+)?|\b\d+(?:\.\d+)?\s*(?:s|seconds?)\b", report), report


def assert_worker(case, executor, report):
    calls = case.calls()
    assert len(calls) == 1, f"Doctor must run one real {executor} subprocess; got {calls}\n{report}"
    call = calls[0]
    assert call["executor"] == executor
    assert len(case.launches) == 1
    assert {"foragent.runner", "foragent.executors"} <= case.launches[0]["modules"], (
        "Doctor must share the issue runner's execution path, not launch an independent stub"
    )
    assert Path(call["cwd"]) != case.root.resolve()
    assert call["git_checkout"], "Use an isolated clone/worktree of the supplied repository"
    assert call["seed"] == case.seed
    assert "hello.txt" in call["delivered"] and "Done when" in call["delivered"]
    assert case.gh == []
    return call


def test_doctor_live_quotes_real_claude_goal_refusal(live_doctor, capsys, monkeypatch):
    from foragent import runner

    # Fault injection changes only the condition's test data. The existing
    # prompt builder, executor adapter, subprocess and result reader stay real.
    condition = runner.goal_condition

    def oversized_condition(acceptance_dir):
        return condition(acceptance_dir) + "\n" + "Preserve the approved workspace settings. " * 150

    monkeypatch.setattr(runner, "goal_condition", oversized_condition)
    monkeypatch.setenv("FA_DOCTOR_LIVE_MODE", "strict")
    code, report = invoke_doctor(live_doctor, capsys, "--executor", "claude",
                                 "--repo-root", str(live_doctor.root))
    call = assert_worker(live_doctor, "claude", report)
    assert max(call["goal_lengths"]) > 4000
    assert code != 0
    assert_report(report, "claude", "FAIL")
    assert call["error"].startswith("Goal condition is limited to 4000 characters")
    assert call["error"] in report, "Preserve the executor's own error text verbatim"


def test_doctor_live_runs_configured_claude_with_turns_and_seconds(live_doctor, capsys):
    # No overrides: select the executor from the actual repo config and use cwd.
    code, report = invoke_doctor(live_doctor, capsys)
    assert code == 0, report
    assert_report(report, "claude", "PASS")
    call = assert_worker(live_doctor, "claude", report)
    assert call["argv"][:1] == ["-p"] and "--bg" not in call["argv"]
    assert call["argv"][call["argv"].index("--output-format") + 1] == "stream-json"
    assert call["goal_lengths"], "Claude must exercise the native /goal path"
    assert call["created"]


def test_doctor_live_executor_override_runs_codex_argv(live_doctor, capsys):
    code, report = invoke_doctor(live_doctor, capsys, "--executor", "codex",
                                 "--repo-root", str(live_doctor.root))
    assert code == 0, report
    assert_report(report, "codex", "PASS")
    call = assert_worker(live_doctor, "codex", report)
    argv = call["argv"]
    assert argv[:3] == ["exec", "-", "-C"]
    assert Path(argv[3]).resolve() == Path(call["cwd"])
    assert argv[4:9] == ["-s", "workspace-write", "--skip-git-repo-check", "--json", "-o"]
    assert "--bg" not in argv and "--permission-mode" not in argv
    assert call["created"]


def repo_snapshot(root):
    return {
        "head": git_command(root, "rev-parse", "HEAD"),
        "branch": git_command(root, "symbolic-ref", "HEAD"),
        "refs": git_command(root, "for-each-ref", "--format=%(refname) %(objectname)"),
        "index": git_command(root, "ls-files", "--stage"),
        "status": git_command(root, "status", "--porcelain=v1", "--untracked-files=all"),
        "worktrees": git_command(root, "worktree", "list", "--porcelain"),
        "files": {path.relative_to(root).as_posix(): path.read_bytes()
                  for path in root.rglob("*") if path.is_file() and ".git" not in path.relative_to(root).parts},
    }


def test_doctor_live_preserves_dirty_repo_and_branches_without_github(live_doctor, capsys):
    root = live_doctor.root
    (root / "staged-notes.txt").write_text("Keep this staged change.\n")
    git_command(root, "add", "staged-notes.txt")
    (root / "staged-notes.txt").write_text("Keep this additional unstaged edit too.\n")
    (root / "hello.txt").write_text("Private untracked work; do not overwrite.\n")
    before = repo_snapshot(root)
    code, report = invoke_doctor(live_doctor, capsys, "--repo-root", str(root))
    assert repo_snapshot(root) == before, "Doctor changed the user's checkout, index or branches"
    assert live_doctor.gh == [], "Doctor must not read or mutate GitHub"
    assert code == 0, report
    assert_report(report, "claude", "PASS")
    assert_worker(live_doctor, "claude", report)


@pytest.mark.parametrize("executor", ["claude", "codex"])
def test_doctor_live_missing_binary_is_clear_failure(live_doctor, capsys, executor):
    (live_doctor.bin / executor).unlink()
    code, report = invoke_doctor(live_doctor, capsys, "--executor", executor,
                                 "--repo-root", str(live_doctor.root))
    assert code != 0
    assert_report(report, executor, "FAIL")
    assert re.search(r"not found|missing|no such file|not installed|unavailable", report, re.I), report
    assert live_doctor.calls() == []
    assert live_doctor.gh == []


def test_doctor_live_checks_task_instead_of_trusting_success_json(live_doctor, capsys, monkeypatch):
    monkeypatch.setenv("FA_DOCTOR_LIVE_MODE", "no-artifact")
    code, report = invoke_doctor(live_doctor, capsys, "--executor", "claude",
                                 "--repo-root", str(live_doctor.root))
    call = assert_worker(live_doctor, "claude", report)
    assert not call["created"] and call["error"] is None
    assert code != 0, "A successful JSON envelope cannot substitute for the Done-when check"
    assert_report(report, "claude", "FAIL")
    assert "hello.txt" in report, "Report which smoke-task check failed"
