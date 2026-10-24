import io
import json
import sys
from pathlib import Path

import pytest

from foragent import statusline


@pytest.fixture(autouse=True)
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return tmp_path


def test_signal_exit_maps_to_shell_status(tmp_path):
    statusline._write_json(statusline.record_path(),
                           {"enabled": True, "previous_command": "printf hi; kill -TERM $$"})
    out = io.BytesIO()
    assert statusline.run(io.BytesIO(b"{}"), out) == 143
    assert out.getvalue() == b"hi"


def test_invalid_input_still_runs_previous_command():
    statusline._write_json(statusline.record_path(), {"enabled": True, "previous_command": "cat"})
    out = io.BytesIO()
    assert statusline.run(io.BytesIO(b"\xffnot json"), out) == 0
    assert out.getvalue() == b"\xffnot json"
    assert not statusline.usage_path().exists()


def test_unreadable_answer_is_not_remembered(tmp_path):
    (tmp_path / "claude").mkdir()

    def no_terminal(prompt):
        raise EOFError

    assert statusline.offer_install(no_terminal) == "unanswered"
    assert not statusline.record_path().exists()


def test_no_claude_folder_skips_without_asking():
    assert statusline.offer_install(lambda prompt: pytest.fail("must not ask")) == "skipped"


def test_install_writes_through_settings_symlink(tmp_path):
    (tmp_path / "claude").mkdir()
    real = tmp_path / "dotfiles/settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo old"}}))
    statusline.claude_settings_path().symlink_to(real)
    assert statusline.offer_install(lambda prompt: "yes") == "installed"
    assert statusline.claude_settings_path().is_symlink()
    assert json.loads(real.read_text())["statusLine"]["command"] == "fa statusline"
    assert statusline.load_record() == {"enabled": True, "previous_command": "echo old"}


def test_already_wrapped_settings_are_not_wrapped_twice(tmp_path):
    (tmp_path / "claude").mkdir()
    statusline.claude_settings_path().write_text(
        json.dumps({"statusLine": {"type": "command", "command": "fa statusline"}}))
    assert statusline.offer_install(lambda prompt: "y") == "installed"
    assert statusline.load_record() == {"enabled": True, "previous_command": None}


WRAPPER_VARIANTS = [
    "fa statusline", "fa  statusline", " fa statusline ", "/opt/homebrew/bin/fa statusline",
    "'/Users/me/my tools/fa' statusline", '"fa" "statusline"', "FOO=1 fa statusline",
    "/usr/bin/python3 -m foragent.cli statusline",
]


@pytest.mark.parametrize("command", WRAPPER_VARIANTS)
def test_wrapper_variants_are_detected(command):
    assert statusline.is_wrapper(command)


@pytest.mark.parametrize("command", [
    "fa status", "fa statusline --extra", "my-fa statusline", "echo fa statusline",
    "bash ~/.claude/statusline.sh", "'unterminated", "",
])
def test_other_commands_are_not_the_wrapper(command):
    assert not statusline.is_wrapper(command)


@pytest.mark.parametrize("command", WRAPPER_VARIANTS)
def test_install_never_saves_a_wrapper_variant_as_previous(tmp_path, command):
    (tmp_path / "claude").mkdir()
    statusline.claude_settings_path().write_text(
        json.dumps({"statusLine": {"type": "command", "command": command}}))
    assert statusline.offer_install(lambda prompt: "y") == "installed"
    assert statusline.load_record() == {"enabled": True, "previous_command": None}


def test_recorded_wrapper_variant_is_not_run(monkeypatch):
    statusline._write_json(statusline.record_path(),
                           {"enabled": True, "previous_command": "fa  statusline"})
    monkeypatch.setattr(statusline.subprocess, "run", lambda *a, **k: pytest.fail("must not recurse"))
    out = io.BytesIO()
    assert statusline.run(io.BytesIO(b"{}"), out) == 0 and out.getvalue() == b""


def test_nested_wrapper_inside_a_script_runs_previous_only_once(tmp_path):
    calls = tmp_path / "calls.txt"
    nested = tmp_path / "status.sh"
    # A user script that itself calls the wrapper module again.
    nested.write_text(f"echo x >> '{calls}'\nprintf mine\n"
                      f"echo '{{}}' | '{sys.executable}' -c "
                      "'import sys; from foragent import statusline; sys.exit(statusline.run())'\n")
    statusline._write_json(statusline.record_path(),
                           {"enabled": True, "previous_command": f"sh '{nested}'"})
    out = io.BytesIO()
    assert statusline.run(io.BytesIO(b"{}"), out) == 0
    assert out.getvalue() == b"mine"
    assert calls.read_text() == "x\n"
