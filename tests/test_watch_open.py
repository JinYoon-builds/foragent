import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from foragent import watch, watch_open
from foragent.config import Config, ConfigError
from foragent.state import StateStore
from foragent.tracker import FakeTracker


@pytest.fixture
def opening(tmp_path, monkeypatch):
    monkeypatch.delenv("FORAGENT_VIEW")
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.setattr(watch_open.shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(watch_open, "_call", lambda argv, root: calls.append((argv, root)))
    return StateStore(tmp_path / "state"), calls


def test_recorded_absolute_orca_path_survives_a_minimal_path(opening, tmp_path, monkeypatch):
    store, calls = opening
    executable = "/Applications/Orca App/bin/orca"
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_original")
    monkeypatch.setattr(watch_open.shutil, "which", lambda name: executable if name == "orca" else None)
    watch_open.open_view(tmp_path, store)
    assert calls[0][0][:5] == [executable, "terminal", "split", "--terminal", "term_original"]

    monkeypatch.delenv("ORCA_TERMINAL_HANDLE")
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(watch_open.shutil, "which", lambda name: None)
    calls.clear()
    watch_open.open_view(tmp_path, store)
    assert calls[0][0] == [executable, "terminal", "show", "--terminal", "term_original"]
    assert calls[1][0][:5] == [executable, "terminal", "split", "--terminal", "term_original"]


def test_terminal_command_preserves_shell_and_applescript_special_characters(opening, tmp_path, monkeypatch):
    store, calls = opening
    root = tmp_path / '한글 "double" \'single\' $HOME `echo nope` \\ newline\nrepo'
    root.mkdir()
    monkeypatch.setattr(watch_open.shutil, "which", lambda name: "/fake/osascript" if name == "osascript" else None)
    watch_open.open_view(root, store)
    argv, cwd = calls[0]
    assert argv[:2] == ["/fake/osascript", "-e"]
    prefix = 'tell application "Terminal" to do script '
    assert argv[2].startswith(prefix)
    command = json.loads(argv[2][len(prefix):])
    assert shlex.split(command)[-4:] == ["gate", "watch", "--repo-root", str(root)]
    assert "--open" not in shlex.split(command)
    assert Path(shlex.split(command)[0]).is_absolute()
    assert cwd == root


@pytest.mark.parametrize("content", ["not a pid", "0", "-1", str(2**100)])
def test_invalid_pid_does_not_prevent_opening(opening, tmp_path, monkeypatch, content):
    store, calls = opening
    store.root.mkdir()
    (store.root / "watch.pid").write_text(content)
    monkeypatch.setattr(watch_open.shutil, "which", lambda name: "/fake/osascript" if name == "osascript" else None)
    watch_open.open_view(tmp_path, store)
    assert len(calls) == 1


def test_live_pid_with_permission_denied_still_suppresses_opening(opening, tmp_path, monkeypatch, capsys):
    store, calls = opening
    store.root.mkdir()
    (store.root / "watch.pid").write_text("123")

    def inaccessible(pid, signal):
        raise PermissionError

    monkeypatch.setattr(watch_open.os, "kill", inaccessible)
    watch_open.open_view(tmp_path, store)
    assert calls == []
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure", [FileNotFoundError("gone\naway"), subprocess.TimeoutExpired("opener", 5)])
def test_open_failure_is_one_localized_nonfatal_line(opening, tmp_path, monkeypatch, capsys, failure):
    store, calls = opening
    monkeypatch.setattr(watch_open.shutil, "which", lambda name: "/fake/osascript" if name == "osascript" else None)

    def fail(argv, root):
        raise failure

    monkeypatch.setattr(watch_open, "_call", fail)
    watch_open.open_view(tmp_path, store, "ko")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert "레인 화면을 열지 못했어요" in captured.err


def test_no_opener_reports_once_without_starting_a_process(opening, tmp_path, capsys):
    store, calls = opening
    watch_open.open_view(tmp_path, store)
    assert calls == []
    assert len(capsys.readouterr().err.splitlines()) == 1


@pytest.mark.parametrize("error", [None, KeyboardInterrupt, "curses", RuntimeError])
def test_interactive_view_removes_pid_on_return_or_error(tmp_path, monkeypatch, error):
    import curses

    store = StateStore(tmp_path / "state")
    pid_file = store.root / "watch.pid"
    monkeypatch.setattr(watch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(watch.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(watch, "collect", lambda *args: object())

    def terminal(*args):
        assert int(pid_file.read_text()) == os.getpid()
        if error:
            raise (curses.error if error == "curses" else error)("closed")

    monkeypatch.setattr(curses, "wrapper", terminal)
    if error in ("curses", RuntimeError):
        with pytest.raises(ConfigError if error == "curses" else RuntimeError):
            watch.run(lambda: {}, FakeTracker(), store, Config(), tmp_path)
    else:
        watch.run(lambda: {}, FakeTracker(), store, Config(), tmp_path)
    assert not pid_file.exists()


def test_once_leaves_an_interactive_views_pid_untouched(tmp_path, monkeypatch, capsys):
    store = StateStore(tmp_path)
    pid_file = tmp_path / "watch.pid"
    pid_file.write_text("123\n")
    monkeypatch.setattr(watch, "collect", lambda *args: object())
    monkeypatch.setattr(watch, "render_frame", lambda *args: "one frame")
    watch.run(lambda: {}, FakeTracker(), store, Config(), tmp_path, once=True)
    assert capsys.readouterr().out == "one frame\n"
    assert pid_file.read_text() == "123\n"
