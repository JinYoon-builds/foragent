import io
import json
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
