"""Acceptance test for statusline-uninstall. Locked by Foragent; do not edit."""

import builtins
import errno
import io
import json
import os
from pathlib import Path
import socket
import subprocess

import pytest

from foragent import statusline


PREVIOUS_COMMAND = "  printf '%s' 'Jin의 이전 status line'  "
BEFORE = '{\r\n\t"env" : {"KEEP": "유지", "label": "fa statusline"},\r\n'.encode()
KEY = b'  "statusLine" \t: '
AFTER = (b'\t"permissions" : { "allow": ["Read"], "deny": ["Write(secret)"] },\r\n'
         b'  "enabledPlugins": {"local@example": true}, "theme" : "dark"\r\n}\r\n')


@pytest.fixture(autouse=True)
def isolated_statusline(tmp_path, monkeypatch):
    home = tmp_path / "home"
    settings_dir = home / ".claude"
    settings_dir.mkdir(parents=True)
    for name, path in (
        ("HOME", home), ("CLAUDE_CONFIG_DIR", settings_dir),
        ("CODEX_HOME", home / ".codex"), ("XDG_DATA_HOME", home / ".local/share"),
        ("XDG_CONFIG_HOME", home / ".config"), ("XDG_CACHE_HOME", home / ".cache"),
    ):
        monkeypatch.setenv(name, str(path))
    monkeypatch.chdir(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Statusline uninstall must not start commands, use the network, or send signals")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    assert statusline.claude_settings_path() == settings_dir / "settings.json"
    assert statusline.record_path() == home / ".local/share/foragent/statusline-wrapper.json"


def settings_bytes(value):
    member = b"" if value is None else KEY + json.dumps(value, ensure_ascii=False).encode() + b",\r\n"
    return BEFORE + member + AFTER


def install(previous=PREVIOUS_COMMAND):
    """Use real installation; exercise uninstall with independently formatted JSON."""
    settings = statusline.claude_settings_path()
    original = None if previous is None else {"type": "command", "command": previous, "padding": 3}
    settings.write_bytes(settings_bytes(original))
    before = json.loads(settings.read_bytes())
    assert statusline.offer_install(lambda _prompt: "yes") == "installed"
    assert statusline.load_record() == {"enabled": True, "previous_command": previous}
    installed = json.loads(settings.read_bytes())
    assert statusline.is_wrapper(installed["statusLine"]["command"])
    # Installation currently reserializes JSON. Preserve its result while giving
    # uninstall CRLF, tabs, Unicode, and inline unrelated settings to retain.
    settings.write_bytes(settings_bytes(installed["statusLine"]))
    return before


def uninstall(capsys):
    from foragent.cli import main

    capsys.readouterr()
    try:
        code = main(["statusline", "--uninstall"])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return (0 if code is None else code), captured.out + captured.err


def snapshot(path):
    if not path.exists():
        return None
    stat = path.stat()
    return path.read_bytes(), stat.st_ino, stat.st_mtime_ns


@pytest.mark.parametrize("wrapper_command", [
    "fa statusline", "FORAGENT_LABEL=local  'fa'   statusline",
], ids=["installed-wrapper", "equivalent-wrapper"])
def test_uninstall_restores_recorded_command_and_preserves_settings(capsys, wrapper_command):
    install()
    settings = statusline.claude_settings_path()
    installed = json.loads(settings.read_bytes())["statusLine"]
    assert statusline.is_wrapper(wrapper_command)
    installed["command"] = wrapper_command
    # Keep edits made after installation as well as the original unrelated keys.
    prefix = BEFORE.replace(b'"KEEP":', b'"ADDED_AFTER_INSTALL": true, "KEEP":') + KEY
    suffix = b",\r\n" + AFTER
    settings.write_bytes(prefix + json.dumps(installed, ensure_ascii=False).encode() + suffix)
    expected = json.loads(settings.read_bytes())
    previous = statusline.load_record()["previous_command"]
    expected["statusLine"]["command"] = previous

    code, message = uninstall(capsys)

    assert code == 0, f"Uninstall must restore the recorded command; got exit {code}: {message}"
    assert json.loads(settings.read_bytes()) == expected, "Restore only the recorded command, preserving every other key"
    raw = settings.read_bytes()
    assert raw.startswith(prefix) and raw.endswith(suffix), "Bytes outside the statusLine value must stay unchanged"
    record = statusline.load_record()
    assert record is not None and record.get("enabled") is False, "Uninstall must mark the record disabled"
    assert "restor" in message.lower() and previous.strip() in message, "Uninstall must say which command it restored"


def test_uninstall_without_previous_command_removes_only_statusline(capsys):
    original = install(previous=None)
    settings = statusline.claude_settings_path()

    code, message = uninstall(capsys)

    assert code == 0, f"Uninstall without a previous command must succeed; got exit {code}: {message}"
    assert json.loads(settings.read_bytes()) == original, "Remove our statusLine key without removing other settings"
    raw = settings.read_bytes()
    assert raw.startswith(BEFORE) and raw.endswith(AFTER), "Removing statusLine must preserve unrelated settings bytes"
    record = statusline.load_record()
    assert record is not None and record.get("enabled") is False, "Removing the wrapper must disable its record"


@pytest.mark.parametrize("previous", [PREVIOUS_COMMAND, None], ids=["restored-command", "removed-key"])
def test_uninstall_is_idempotent(capsys, previous):
    install(previous)
    code, message = uninstall(capsys)
    assert code == 0, f"First uninstall must succeed before checking idempotence; got exit {code}: {message}"
    settings, record = statusline.claude_settings_path(), statusline.record_path()
    unchanged = snapshot(settings), snapshot(record)

    code, message = uninstall(capsys)

    assert code == 0, f"Second uninstall must succeed; got exit {code}: {message}"
    assert (snapshot(settings), snapshot(record)) == unchanged, "Second uninstall must not rewrite settings or its record"
    assert any(text in message.lower() for text in (
        "nothing installed", "nothing is installed", "not installed", "already uninstalled", "no wrapper installed",
    )), "Second uninstall must explain that nothing is installed"


@pytest.mark.parametrize("changed", [
    {"type": "command", "command": "printf '%s' 'fa statusline is just text'", "padding": 9},
    None,
], ids=["user-command", "user-removed-key"])
def test_uninstall_preserves_user_changed_statusline(capsys, changed):
    install()
    assert changed is None or not statusline.is_wrapper(changed["command"])
    settings = statusline.claude_settings_path()
    settings.write_bytes(settings_bytes(changed))
    unchanged = snapshot(settings)

    code, message = uninstall(capsys)

    assert code == 0, f"A user-changed statusLine must be a non-error; got exit {code}: {message}"
    assert snapshot(settings) == unchanged, "Uninstall must not write settings after the user changes or removes statusLine"
    assert "user" in message.lower() and any(text in message.lower() for text in (
        "changed", "modified", "replaced", "removed",
    )), "Uninstall must explain that statusLine was changed by the user"


def test_uninstall_missing_settings_reports_error_without_writing(capsys):
    install()
    settings, record = statusline.claude_settings_path(), statusline.record_path()
    settings.unlink()
    unchanged = snapshot(record)

    code, message = uninstall(capsys)

    assert not settings.exists(), "Uninstall must not recreate missing settings.json"
    assert snapshot(record) == unchanged, "Uninstall must not change its record when settings.json is missing"
    assert code != 0, "Missing settings.json must produce a non-zero exit"
    assert "settings.json" in message and any(text in message.lower() for text in (
        "missing", "not found", "no such", "does not exist", "cannot read", "could not read", "unable to read",
    )), f"Uninstall must report missing settings.json, not reject --uninstall: {message}"


def test_uninstall_unreadable_settings_reports_error_without_writing(capsys, monkeypatch):
    install()
    settings, record = statusline.claude_settings_path(), statusline.record_path()
    unchanged = snapshot(settings), snapshot(record)
    attempted_reads = []

    def deny_settings_read(open_file):
        def guarded(file, mode="r", *args, **kwargs):
            if not isinstance(file, int) and Path(file) == settings and ("r" in mode or "+" in mode):
                attempted_reads.append(file)
                raise PermissionError(errno.EACCES, "Permission denied", str(settings))
            return open_file(file, mode, *args, **kwargs)

        return guarded

    # chmod alone is ineffective under root. Deny only this temporary file's
    # reads at the I/O boundary, covering both open() and pathlib's io.open().
    with monkeypatch.context() as denied:
        denied.setattr(builtins, "open", deny_settings_read(builtins.open))
        denied.setattr(io, "open", deny_settings_read(io.open))
        code, message = uninstall(capsys)

    assert (snapshot(settings), snapshot(record)) == unchanged, "Unreadable settings.json must cause no writes"
    assert code != 0, "Unreadable settings.json must produce a non-zero exit"
    assert "settings.json" in message and any(text in message.lower() for text in (
        "permission", "denied", "unreadable", "not readable", "cannot read", "could not read", "unable to read",
    )), f"Uninstall must report unreadable settings.json, not reject --uninstall: {message}"
    assert attempted_reads, "The unreadable-settings scenario must exercise a denied settings read"
