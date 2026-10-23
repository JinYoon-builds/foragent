"""Acceptance contract: view-open-terminals, using the existing opener recorder.

Orca takes priority over tmux; tmux takes priority over an application window.
Pair the already-supported Orca path with recovery to tmux so this regression
scenario, like the other scenarios here, fails on the current implementation.
"""

import shlex
import sys

from test_view_open import assert_opened, view_case


def terminal_case(git_history, tmp_path, monkeypatch):
    case, main, commands = view_case(git_history, tmp_path, monkeypatch)
    directory = commands.path.parent / "bin"
    # Extend fake_commands with tmux while preserving its subprocess JSON log.
    # No real terminal, pane, or watcher is created by any recorded command.
    recorder = (
        "import json, os, sys; from pathlib import Path; "
        "row = {'executable': 'tmux', 'argv': sys.argv[1:], 'cwd': os.getcwd()}; "
        "stream = Path(os.environ['FA_D57_LOG']).open('a'); "
        "stream.write(json.dumps(row) + '\\n'); stream.close()"
    )
    (directory / "tmux").write_text(
        "#!/bin/sh\nexec " + shlex.join([sys.executable, "-c", recorder]) + ' "$@"\n')
    (directory / "tmux").chmod(0o755)
    # Do not discover host-installed openers or inherit the runner's terminal.
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    return case, main, commands


def assert_watch_command(command, root):
    assert "gate watch" in command and "--repo-root" in command and str(root.resolve()) in command
    assert "--open" not in command, "The new view must not recursively open another view"


def assert_tmux_split(commands, root):
    calls = commands.calls("tmux")
    assert len(calls) == 1, f"Expected one tmux split-window -h; recorded {commands.calls()}"
    argv = calls[0]["argv"]
    assert argv[:2] == ["split-window", "-h"], f"Use a horizontal tmux split: {argv}"
    assert_watch_command(" ".join(argv[2:]), root)
    assert commands.splits() == [] and commands.calls("osascript") == [], (
        "A tmux view must stay in a split pane without opening an app window")


def test_inside_tmux_opens_horizontal_pane_instead_of_an_application_window(
    git_history, tmp_path, monkeypatch,
):
    case, main, commands = terminal_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-offline/default,123,0")
    # Even an iTerm-hosted tmux session should use a pane in that session.
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert main(["gate", "watch", "--open", "--repo-root", str(case.root)]) == 0
    assert_tmux_split(commands, case.root)


def test_iterm_session_uses_iterm_applescript_and_never_terminal_app(
    git_history, tmp_path, monkeypatch,
):
    case, main, commands = terminal_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert main(["gate", "watch", "--open", "--repo-root", str(case.root)]) == 0
    calls = commands.calls("osascript")
    assert len(calls) == 1, f"Expected one iTerm AppleScript launch; got {commands.calls()}"
    script = " ".join(calls[0]["argv"])
    assert 'tell application "iTerm' in script, f"iTerm session must open via iTerm AppleScript: {script}"
    assert 'tell application "Terminal"' not in script
    assert_watch_command(script, case.root)
    assert commands.calls("tmux") == [] and commands.splits() == []


def test_orca_wins_then_a_dead_recorded_terminal_falls_back_to_tmux(
    git_history, tmp_path, monkeypatch,
):
    case, main, commands = terminal_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_test1")
    monkeypatch.setenv("TMUX", "/tmp/tmux-offline/default,123,0")
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert main(["gate", "watch", "--open"]) == 0
    assert_opened(commands, case.root, terminal="term_test1")
    assert commands.calls("tmux") == [], "An explicit Orca terminal takes priority"
    commands.clear()
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE")
    monkeypatch.setenv("FA_D57_SHOW_STATUS", "1")
    assert main(["gate", "watch", "--open"]) == 0
    calls = commands.calls("orca")
    assert calls and calls[0]["argv"] == ["terminal", "show", "--terminal", "term_test1"]
    assert_tmux_split(commands, case.root)


def test_no_opener_prints_one_exact_manual_command_without_affecting_exit(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main, commands = terminal_case(git_history, tmp_path, monkeypatch)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    capsys.readouterr()
    assert main(["gate", "watch", "--open", "--repo-root", str(case.root)]) == 0
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).strip().splitlines()
    assert len(lines) == 1, f"Missing openers should produce one helpful line; got {lines}"
    command = shlex.join(["fa", "gate", "watch", "--repo-root", str(case.root.resolve())])
    assert command in lines[0], f"Fallback must print the exact runnable command {command!r}; got {lines[0]!r}"
    assert "Traceback" not in lines[0]
    assert commands.calls() == [], "No opener must mean no attempted external process"
