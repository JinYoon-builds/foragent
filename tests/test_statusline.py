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


def test_failed_settings_write_is_not_remembered_and_next_init_installs(tmp_path, monkeypatch):
    (tmp_path / "claude").mkdir()
    settings = statusline.claude_settings_path()
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo old"}}))
    real_write = statusline._write_json

    def settings_unwritable(path, value):
        if path.name == "settings.json":
            raise PermissionError(13, "Permission denied", str(path))
        real_write(path, value)

    monkeypatch.setattr(statusline, "_write_json", settings_unwritable)
    with pytest.raises(PermissionError):
        statusline.offer_install(lambda prompt: "y")
    assert statusline.load_record() is None
    assert json.loads(settings.read_text())["statusLine"]["command"] == "echo old"
    monkeypatch.setattr(statusline, "_write_json", real_write)
    assert statusline.offer_install(lambda prompt: "y") == "installed"
    assert json.loads(settings.read_text())["statusLine"]["command"] == "fa statusline"
    assert statusline.load_record() == {"enabled": True, "previous_command": "echo old"}


def test_init_reports_failed_install_without_crashing(tmp_path, monkeypatch, capsys):
    from foragent import cli

    def unwritable(ask):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(statusline, "offer_install", unwritable)
    cli._offer_statusline("yes")
    assert "ask again" in capsys.readouterr().out


def test_concurrent_init_keeps_the_first_install_and_its_previous_command(tmp_path):
    """A second init that asked before the first finished must not save the wrapper as previous."""
    (tmp_path / "claude").mkdir()
    settings = statusline.claude_settings_path()
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo old"}}))

    def answer_while_other_init_installs(prompt):
        assert statusline.offer_install(lambda prompt: "y") == "installed"
        return "y"

    assert statusline.offer_install(answer_while_other_init_installs) == "kept"
    assert statusline.load_record() == {"enabled": True, "previous_command": "echo old"}
    assert json.loads(settings.read_text())["statusLine"]["command"] == "fa statusline"


def test_concurrent_refusal_does_not_undo_an_install(tmp_path):
    (tmp_path / "claude").mkdir()
    statusline.claude_settings_path().write_text(json.dumps({}))

    def refuse_after_other_init_installs(prompt):
        assert statusline.offer_install(lambda prompt: "y") == "installed"
        return "n"

    assert statusline.offer_install(refuse_after_other_init_installs) == "kept"
    assert statusline.load_record() == {"enabled": True, "previous_command": None}


def test_install_waits_for_another_process_holding_the_lock(tmp_path):
    import subprocess

    (tmp_path / "claude").mkdir()
    settings = statusline.claude_settings_path()
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo old"}}))
    # The other process records an install inside the lock, then holds it a moment longer.
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import time; from foragent import statusline\n"
         "with statusline._install_lock():\n"
         "    print('held', flush=True)\n"
         "    time.sleep(0.3)\n"
         "    statusline._write_json(statusline.record_path(), "
         "{'enabled': True, 'previous_command': 'echo old'})\n"
         "    statusline._write_json(statusline.claude_settings_path(), "
         "{'statusLine': {'type': 'command', 'command': 'fa statusline'}})\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert statusline.offer_install(lambda prompt: "y") == "kept"
    finally:
        assert holder.wait(timeout=15) == 0
    assert statusline.load_record() == {"enabled": True, "previous_command": "echo old"}


def _installed(settings_text, previous):
    statusline.claude_settings_path().parent.mkdir(parents=True, exist_ok=True)
    statusline.claude_settings_path().write_text(settings_text, encoding="utf-8")
    statusline._write_json(statusline.record_path(), {"enabled": True, "previous_command": previous})


@pytest.mark.parametrize("before, after", [
    ('{"theme": "dark",\n  "statusLine": {"type": "command", "command": "fa statusline"}\n}\n',
     '{"theme": "dark"\n}\n'),
    ('{ "statusLine": {"type": "command", "command": "fa statusline"} }', '{ }'),
    ('{"statusLine": 1, "a": 2, "statusLine": {"type": "command", "command": "fa statusline"}}',
     '{ "a": 2}'),
    ('{"statusLine": {"type": "command", "command": "fa statusline"},\n "a": 2,\n'
     ' "statusLine": {"type": "command", "command": "fa statusline"}}',
     '{\n "a": 2}'),
], ids=["last-member", "only-member", "duplicate-key", "duplicate-wrappers"])
def test_uninstall_removes_statusline_member_anywhere(before, after):
    _installed(before, None)
    code, message = statusline.uninstall()
    assert code == 0, message
    assert statusline.claude_settings_path().read_text(encoding="utf-8") == after
    assert statusline.load_record() == {"enabled": False, "previous_command": None}


def test_uninstall_rejects_invalid_settings_without_writing():
    _installed('{"statusLine": {"type": "command", "command": "fa statusline"},}', "true")
    code, message = statusline.uninstall()
    assert code == 1 and "settings.json" in message and "could not read" in message
    assert statusline.load_record()["enabled"] is True


def test_uninstall_speaks_korean():
    _installed('{"statusLine": {"type": "command", "command": "fa statusline"}}', "echo hi")
    code, message = statusline.uninstall("ko")
    assert code == 0 and message == "예전 Claude 상태 줄로 되돌렸어요: echo hi"
    assert json.loads(statusline.claude_settings_path().read_text()) == {
        "statusLine": {"type": "command", "command": "echo hi"}}


def test_uninstall_restore_with_duplicates_activates_the_previous_command():
    _installed('{"statusLine": {"type": "command", "command": "fa statusline"}, '
               '"statusLine": {"type": "command", "command": "fa statusline"}}', "echo hi")
    code, message = statusline.uninstall()
    assert code == 0, message
    assert json.loads(statusline.claude_settings_path().read_text())["statusLine"]["command"] == "echo hi"
