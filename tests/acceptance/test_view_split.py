"""Acceptance test for view-split. Locked by Foragent; do not edit."""

import pytest

from test_skills_autosync import isolated_home


def opening(tmp_path, monkeypatch, environment, executables):
    from foragent import watch_open
    from foragent.state import StateStore

    for key in ("FORAGENT_VIEW", "ORCA_TERMINAL_HANDLE", "TMUX", "WEZTERM_PANE",
                "KITTY_WINDOW_ID", "KITTY_LISTEN_ON", "TERM_PROGRAM"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    calls = []
    monkeypatch.setattr(watch_open, "_call", lambda argv, root: calls.append((argv, root)))
    monkeypatch.setattr(watch_open.shutil, "which", lambda name: str(tmp_path / "bin" / name)
                        if name in executables else None)
    root = tmp_path / "repo with spaces"
    root.mkdir()
    watch_open.open_view(root, StateStore(tmp_path / "state"))
    assert len(calls) == 1, calls
    argv, cwd = calls[0]
    assert cwd == root
    command = " ".join(argv)
    assert "gate watch" in command and "--repo-root" in command and str(root) in command
    assert "--open" not in command
    return argv


def test_view_split_orca_uses_vertical_direction(tmp_path, monkeypatch):
    argv = opening(tmp_path, monkeypatch, {"ORCA_TERMINAL_HANDLE": "term_test"}, {"orca"})
    assert argv[1:3] == ["terminal", "split"]
    assert argv[argv.index("--terminal") + 1] == "term_test"
    assert argv[argv.index("--direction") + 1] == "vertical"


@pytest.mark.parametrize("environment,executable,prefix,required", [
    ({"TMUX": "/tmp/offline-tmux,1,0"}, "tmux", ["split-window"], ["-h"]),
    ({"WEZTERM_PANE": "12"}, "wezterm", ["cli", "split-pane"], ["--right", "--"]),
    ({"KITTY_WINDOW_ID": "3", "KITTY_LISTEN_ON": "unix:/tmp/offline-kitty"},
     "kitty", ["@"], ["launch", "--location=vsplit"]),
])
def test_view_split_supported_multiplexers_create_side_by_side_panes(
    tmp_path, monkeypatch, environment, executable, prefix, required,
):
    argv = opening(tmp_path, monkeypatch, environment, {executable, "osascript"})
    assert argv[0].endswith("/" + executable)
    assert argv[1:1 + len(prefix)] == prefix
    assert all(option in argv for option in required)


def test_view_split_iterm_splits_vertically(tmp_path, monkeypatch):
    argv = opening(tmp_path, monkeypatch, {"TERM_PROGRAM": "iTerm.app"}, {"osascript"})
    assert argv[0].endswith("/osascript")
    assert "split vertically" in " ".join(argv)


def test_view_split_apple_terminal_opens_a_new_window(tmp_path, monkeypatch):
    argv = opening(tmp_path, monkeypatch, {"TERM_PROGRAM": "Apple_Terminal"}, {"osascript"})
    assert argv[0].endswith("/osascript")
    assert "do script" in " ".join(argv)
    assert 'application "Terminal"' in " ".join(argv)
