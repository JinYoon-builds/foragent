"""Acceptance test for view-open. Locked by Foragent; do not edit."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

import pytest


class CommandLog:
    def __init__(self, path, tool_dir):
        self.path, self.tool_dir = path, tool_dir

    def calls(self, executable=None):
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        return [row for row in rows if executable is None or row["executable"] == executable]

    def splits(self):
        return [row for row in self.calls("orca") if row["argv"][:2] == ["terminal", "split"]]

    def clear(self):
        self.path.write_text("")


def fake_commands(tmp_path, monkeypatch):
    """Use real subprocesses, with all desktop/installer effects confined to tmp_path."""
    directory = tmp_path / "bin"
    directory.mkdir()
    log = tmp_path / "commands.jsonl"
    log.write_text("")
    tool_dir = tmp_path / "tools"
    driver = directory / "record.py"
    driver.write_text('''import json
import os
from pathlib import Path
import shutil
import sys

executable = Path(sys.argv[1]).name
args = sys.argv[2:]
row = {"executable": executable, "argv": args, "cwd": os.getcwd()}
install = executable == "uv" and args[:2] == ["tool", "install"]
if install:
    source = Path(args[-1]).resolve()
    package = source / "src/foragent/__init__.py"
    row["source_root"] = str(source)
    row["source_init"] = package.read_text() if package.is_file() else None
with Path(os.environ["FA_D57_LOG"]).open("a") as stream:
    stream.write(json.dumps(row) + "\\n")
if executable == "orca" and args[:2] == ["terminal", "show"]:
    sys.exit(int(os.environ["FA_D57_SHOW_STATUS"]))
if executable in {"orca", "osascript"}:
    sys.exit(int(os.environ["FA_D57_OPENER_STATUS"]))
if executable == "uv" and args == ["tool", "dir"]:
    print(os.environ["FA_D57_TOOL_DIR"])
elif install:
    mode = os.environ["FA_D57_INSTALL_MODE"]
    if mode == "error":
        print("offline installer failed", file=sys.stderr)
        sys.exit(1)
    destination = Path(os.environ["FA_D57_TOOL_DIR"]) / "foragent/lib/python3.12/site-packages/foragent"
    destination.mkdir(parents=True, exist_ok=True)
    if mode == "match":
        shutil.copytree(source / "src/foragent", destination, dirs_exist_ok=True)
    else:
        (destination / "__init__.py").write_text("BUILD = 'wrong installed version'\\n")
else:
    raise SystemExit("unexpected fake command: " + repr(sys.argv))
''')
    wrapper = f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(driver))} \"$0\" \"$@\"\n"
    for executable in ("orca", "osascript", "uv"):
        path = directory / executable
        path.write_text(wrapper)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FA_D57_LOG", str(log))
    monkeypatch.setenv("FA_D57_TOOL_DIR", str(tool_dir))
    monkeypatch.setenv("FA_D57_SHOW_STATUS", "0")
    monkeypatch.setenv("FA_D57_OPENER_STATUS", "0")
    monkeypatch.setenv("FA_D57_INSTALL_MODE", "match")
    monkeypatch.delenv("FORAGENT_VIEW", raising=False)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    # Premerge and any installer worktrees also stay within the test sandbox.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return CommandLog(log, tool_dir)


def view_case(git_history, tmp_path, monkeypatch):
    from test_orch_review_handoff import bind_cli, make_case

    commands = fake_commands(tmp_path, monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch)
    return case, bind_cli(case, monkeypatch), commands


def assert_opened(commands, root, *, terminal=None):
    if terminal is None:
        assert commands.splits() == []
        calls = commands.calls("osascript")
        assert len(calls) == 1, "lane view must open one Terminal window"
        command = " ".join(calls[0]["argv"])
        assert "Terminal" in command and "do script" in command
    else:
        calls = commands.splits()
        assert len(calls) == 1, "lane view must open exactly one Orca split"
        assert commands.calls("osascript") == []
        argv = calls[0]["argv"]
        assert argv[argv.index("--terminal") + 1] == terminal
        command = argv[argv.index("--command") + 1]
    assert "gate watch" in command and "--repo-root" in command and str(root) in command
    assert "--open" not in command, "the launched view must not recursively launch another view"
    assert Path(calls[0]["cwd"]).is_dir()


@pytest.mark.parametrize("terminal", ["term_test1", None])
def test_open_returns_after_launching_view_in_orca_or_terminal(
    git_history, tmp_path, monkeypatch, terminal,
):
    case, main, commands = view_case(git_history, tmp_path, monkeypatch)
    if terminal:
        monkeypatch.setenv("ORCA_TERMINAL_HANDLE", terminal)
    # Cover both the cwd default and explicit repository selection.
    argv = ["gate", "watch", "--open"] + ([] if terminal else ["--repo-root", str(case.root)])
    assert main(argv) == 0
    assert_opened(commands, case.root, terminal=terminal)


@pytest.mark.parametrize("show_status", [0, 1], ids=["recorded-alive", "recorded-dead"])
def test_recorded_orca_terminal_is_probed_before_split_or_new_window(
    git_history, tmp_path, monkeypatch, show_status,
):
    case, main, commands = view_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_test1")
    assert main(["gate", "watch", "--open"]) == 0
    assert_opened(commands, case.root, terminal="term_test1")
    commands.clear()
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.setenv("FA_D57_SHOW_STATUS", str(show_status))
    assert main(["gate", "watch", "--open", "--repo-root", str(case.root)]) == 0
    calls = commands.calls("orca")
    assert calls and calls[0]["argv"] == ["terminal", "show", "--terminal", "term_test1"]
    assert sum(row["argv"][:2] == ["terminal", "show"] for row in calls) == 1
    assert_opened(commands, case.root, terminal="term_test1" if show_status == 0 else None)


def test_running_view_registers_live_pid_and_a_dead_pid_allows_reopening(
    git_history, tmp_path, monkeypatch,
):
    import curses

    case, main, commands = view_case(git_history, tmp_path, monkeypatch)
    # bind_cli injects StateStore.for_repo, exactly the gate CLI's state resolution.
    pid_file = case.store.root / "watch.pid"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    entered = []

    def terminal(*args, **kwargs):
        entered.append(True)
        assert pid_file.is_file(), "running lane view must write watch.pid in the gate StateStore"
        assert int(pid_file.read_text()) == os.getpid()
        assert main(["gate", "watch", "--open"]) == 0
        assert commands.calls() == [], "a live view must suppress all opener subprocesses"
        raise KeyboardInterrupt

    monkeypatch.setattr(curses, "wrapper", terminal)
    assert main(["gate", "watch"]) == 0
    assert entered == [True]
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=5)
    pid_file.write_text(str(process.pid))
    assert main(["gate", "watch", "--open"]) == 0
    assert_opened(commands, case.root)


@pytest.mark.parametrize("terminal", ["term_test1", None])
def test_opt_out_is_silent_and_opener_failure_is_a_nonfatal_one_line_note(
    git_history, tmp_path, monkeypatch, capsys, terminal,
):
    case, main, commands = view_case(git_history, tmp_path, monkeypatch)
    if terminal:
        monkeypatch.setenv("ORCA_TERMINAL_HANDLE", terminal)
    # Pair the negative contract with an attempted open, so absent hooks cannot pass.
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    assert main(["gate", "watch", "--open"]) == 0
    assert commands.calls() == []
    assert capsys.readouterr() == ("", "")
    monkeypatch.delenv("FORAGENT_VIEW", raising=False)
    monkeypatch.setenv("FA_D57_OPENER_STATUS", "1")
    assert main(["gate", "watch", "--open"]) == 0
    assert commands.splits() if terminal else commands.calls("osascript")
    captured = capsys.readouterr()
    assert len((captured.out + captured.err).strip().splitlines()) == 1
