"""Acceptance contract: the lane view opens as a split beside the PM in every terminal that can split.

`fa gate watch --open` runs for real. Terminal detection is faked through the
environment, and every opener (osascript, tmux, ghostty, code, cursor) is a
recorder on PATH, so no real terminal, window or view process starts.
"""

from pathlib import Path
import re

import pytest

from test_view_open_terminals import assert_watch_command, terminal_case


ROOT = Path(__file__).resolve().parents[2]
HOST_TERMINAL = ("TMUX", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "WEZTERM_PANE", "KITTY_WINDOW_ID",
                 "KITTY_LISTEN_ON", "ITERM_SESSION_ID", "GHOSTTY_RESOURCES_DIR", "GHOSTTY_BIN_DIR",
                 "VSCODE_PID", "VSCODE_IPC_HOOK_CLI", "ORCA_TERMINAL_HANDLE", "FORAGENT_VIEW")


def split_case(git_history, tmp_path, monkeypatch):
    case, main, commands = terminal_case(git_history, tmp_path, monkeypatch)
    for key in HOST_TERMINAL:
        monkeypatch.delenv(key, raising=False)
    directory = commands.path.parent / "bin"
    # Editor and Ghostty CLIs are recorders too: any call to them shows up in the log.
    wrapper = (directory / "osascript").read_text()
    for name in ("ghostty", "code", "cursor"):
        (directory / name).write_text(wrapper)
        (directory / name).chmod(0o755)
    return case, main, commands


def opened(main, case, commands):
    assert main(["gate", "watch", "--open", "--repo-root", str(case.root)]) == 0
    calls = commands.calls()
    assert len(calls) == 1, f"--open must run exactly one opener command; recorded {calls}"
    return calls[0]


@pytest.mark.parametrize("environment,application", [
    ({"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": "w0t0p0:7E1F0000-0000-4000-8000-000000000001"},
     "iTerm"),
    ({"TERM_PROGRAM": "ghostty", "GHOSTTY_RESOURCES_DIR": "/Applications/Ghostty.app/Contents/Resources/ghostty"},
     "Ghostty"),
], ids=["iterm2", "ghostty"])
def test_scriptable_terminal_splits_the_current_pane_instead_of_opening_a_window(
    git_history, tmp_path, monkeypatch, environment, application,
):
    case, main, commands = split_case(git_history, tmp_path, monkeypatch)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    call = opened(main, case, commands)
    assert call["executable"] == "osascript", f"{application} is driven by its AppleScript dictionary: {call}"
    script = " ".join(call["argv"])
    assert f'application "{application}' in script, f"Talk to {application} itself: {script}"
    assert 'application "Terminal"' not in script, f"{application} must not fall back to Terminal.app"
    assert re.search(r"\bsplit\b", script), f"{application} must split the current pane: {script}"
    assert not re.search(r"new (?:window|tab)", script), f"A split is not a new window or tab: {script}"
    assert_watch_command(script, case.root)


def test_editor_terminal_without_a_split_command_falls_back_to_exactly_one_window(
    git_history, tmp_path, monkeypatch,
):
    """VS Code and Cursor both set TERM_PROGRAM=vscode and offer no CLI to split their terminal."""
    case, main, commands = split_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    monkeypatch.setenv("VSCODE_PID", "4242")
    call = opened(main, case, commands)
    assert call["executable"] == "osascript", f"The fallback is one Terminal window, not the editor CLI: {call}"
    script = " ".join(call["argv"])
    assert 'application "Terminal"' in script and "do script" in script, script
    assert_watch_command(script, case.root)


def test_ghostty_inside_tmux_still_splits_the_tmux_pane(git_history, tmp_path, monkeypatch):
    case, main, commands = split_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    monkeypatch.setenv("TMUX", "/tmp/tmux-offline/default,123,0")
    call = opened(main, case, commands)
    assert call["executable"] == "tmux" and call["argv"][:2] == ["split-window", "-h"], (
        f"The innermost multiplexer owns the PM's pane: {call}")
    assert_watch_command(" ".join(call["argv"][2:]), case.root)


def test_fa_session_start_opens_the_view_with_the_open_command_not_only_while_work_runs():
    skill = (ROOT / "skills/fa/SKILL.md").read_text(encoding="utf-8")
    steps = re.split(r"\n(?=\d+\. )", skill)
    opening = [step for step in steps if "fa gate watch --open" in step]
    assert opening, "/fa must open the lane view through `fa gate watch --open`"
    assert any(not re.search(r"work is running|`working`|`queued`", step) for step in opening), (
        "/fa must open the view at session start, not only when work is running")
    assert not re.search(r"orca terminal (?:create|new)|new tab|새 탭", skill, re.I), (
        "/fa must not open the view in a separate tab")
