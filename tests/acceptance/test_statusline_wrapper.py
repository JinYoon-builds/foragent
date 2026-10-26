"""Acceptance test for statusline-wrapper. Locked by Foragent; do not edit.

Account-wide files under $XDG_DATA_HOME/foragent:
  claude-rate-limits.json = {seen_at: ISO UTC, rate_limits: unchanged input object}
  statusline-wrapper.json = {enabled: bool, previous_command: str | null}
The second file remembers init's answer, including a refusal. Claude's native
~/.claude/settings.json uses statusLine.type=command, command='fa statusline'.
Runtime is repository-independent; stdout is a byte stream, including no final
newline, ANSI sequences and non-UTF-8 bytes. All homes and external commands are
offline fixtures; init uses the existing public Git/GitHub dependency injection.
"""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from test_init import setup
from test_skills_autosync import cli_exit, isolated_home

def _skip_upstream(answer):
    """#419 asks once about upstream reports; these tests count only statusline questions."""
    return lambda prompt="": "n" if "upstream report" in prompt.lower() else answer(prompt)



RATE_LIMITS = {
    "five_hour": {"used_percentage": 23.5, "resets_at": 1793082762},
    "seven_day": {"used_percentage": 67.0, "resets_at": 1793210400},
}
OUTPUT = b"\x1b[32mClaude \xe2\x9c\x93\x1b[0m\r\n\xff  no final newline"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, isolated_home):
    from foragent import executors

    def forbidden(*args, **kwargs):
        pytest.fail("Statusline setup must never run a model or signal a real process")

    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setenv("CODEX_HOME", str(isolated_home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated_home / ".claude"))
    # Also protect the CLI subprocess, where monkeypatches do not propagate.
    bin_root = isolated_home / "bin"
    bin_root.mkdir()
    for tool in ("codex", "claude"):
        path = bin_root / tool
        path.write_text(f"#!{sys.executable}\nraise SystemExit('No model turns allowed')\n", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_root) + os.pathsep + os.environ["PATH"])


def data_paths():
    root = Path(os.environ["XDG_DATA_HOME"]) / "foragent"
    return root / "claude-rate-limits.json", root / "statusline-wrapper.json"


def settings_path():
    return Path.home() / ".claude/settings.json"


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def install_record(previous_command):
    _, record = data_paths()
    save_json(record, {"enabled": True, "previous_command": previous_command})
    save_json(settings_path(), {"statusLine": {"type": "command", "command": "fa statusline"}})


def previous_statusline(tmp_path, *, exit_code=0, expects_usage=True):
    """An actual user command with spaces/arguments; records exact stdin and calls."""
    script = tmp_path / "my statusline.py"
    captured, calls = tmp_path / "stdin.bin", tmp_path / "calls.txt"
    usage, _ = data_paths()
    script.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        f"raw = sys.stdin.buffer.read()\nPath({str(captured)!r}).write_bytes(raw)\n"
        f"with Path({str(calls)!r}).open('a') as stream:\n    stream.write('called\\n')\n"
        "assert sys.argv[1:] == ['--label', 'my status line']\n"
        + (f"assert json.loads(Path({str(usage)!r}).read_text())['rate_limits'] == json.loads(raw)['rate_limits']\n"
           if expects_usage else "")
        + f"sys.stdout.buffer.write({OUTPUT!r})\nsys.stdout.buffer.flush()\nraise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(script), "--label", "my status line"])
    return command, captured, calls


def run_statusline(tmp_path, raw):
    """Exercise the shipped CLI without a project or a model in the subprocess."""
    env = os.environ.copy()
    source = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-m", "foragent.cli", "statusline"],
                          input=raw, capture_output=True, cwd=tmp_path, env=env, timeout=15)


def statusline_input():
    # Forward the original bytes, not JSON reserialized by the wrapper.
    return ("  " + json.dumps({"model": {"display_name": "클로드"}, "rate_limits": RATE_LIMITS},
                             ensure_ascii=False, indent=2) + " \n").encode("utf-8")


@pytest.mark.parametrize("exit_code", [0, 23])
def test_statusline_wrapper_records_usage_and_preserves_command_io_and_exit(tmp_path, exit_code):
    previous, captured, calls = previous_statusline(tmp_path, exit_code=exit_code)
    install_record(previous)
    raw = statusline_input()
    before = datetime.now(UTC)
    result = run_statusline(tmp_path, raw)
    after = datetime.now(UTC)
    assert result.returncode == exit_code, "fa statusline must return the previous command's exit code"
    assert result.stdout == OUTPUT, "statusline stdout must be byte-identical, without decorations or decoding"
    assert captured.read_bytes() == raw, "the previous command must receive the original stdin bytes"
    assert calls.read_text() == "called\n", "the previous command must run exactly once"
    usage, _ = data_paths()
    snapshot = json.loads(usage.read_text())
    assert snapshot["rate_limits"] == RATE_LIMITS
    seen = datetime.fromisoformat(snapshot["seen_at"])
    assert seen.utcoffset().total_seconds() == 0
    assert before <= seen <= after, "seen_at must record observation time, not a reset deadline"


def test_statusline_wrapper_without_previous_command_prints_nothing(tmp_path):
    install_record(None)
    result = run_statusline(tmp_path, statusline_input())
    assert result.returncode == 0, "fa statusline must work with no previous statusline"
    assert result.stdout == b"", "collecting usage must not add anything to the user's statusline"
    usage, _ = data_paths()
    snapshot = json.loads(usage.read_text())
    assert snapshot["rate_limits"] == RATE_LIMITS
    assert datetime.fromisoformat(snapshot["seen_at"]).tzinfo is not None


@pytest.mark.parametrize("existing_snapshot", [False, True], ids=["absent", "preserved"])
def test_statusline_wrapper_without_rate_limits_leaves_usage_untouched_and_calls_previous(
    tmp_path, existing_snapshot,
):
    previous, captured, calls = previous_statusline(tmp_path, exit_code=23, expects_usage=False)
    install_record(previous)
    usage, _ = data_paths()
    original = b'{"seen_at":"2026-10-20T09:00:00+00:00", "rate_limits":{}}\n'
    if existing_snapshot:
        usage.write_bytes(original)
    raw = b' { "model": {"display_name": "Claude"} } \n'
    result = run_statusline(tmp_path, raw)
    assert result.returncode == 23, "missing rate_limits must still call the previous command"
    assert result.stdout == OUTPUT
    assert captured.read_bytes() == raw and calls.read_text() == "called\n"
    if existing_snapshot:
        assert usage.read_bytes() == original, "missing rate_limits must not refresh or erase the last observation"
    else:
        assert not usage.exists(), "input without rate_limits must not create a usage file"


def init_cli(setup, monkeypatch):
    from foragent import cli

    # Keep real init logic. The existing fixture injects offline Git/GitHub facts
    # and replaces forbidden commit/push subprocesses with temporary fast-import.
    monkeypatch.setattr(cli, "init_repo", lambda root, **kwargs: setup.init(**kwargs))
    return cli.main, ["init", "--repo-root", str(setup.root), "--approver", "human",
                      "--executor", "claude", "--no-push", "--no-launchd", "--no-board"]


@pytest.mark.parametrize("has_previous", [True, False], ids=["existing-command", "no-command"])
def test_statusline_wrapper_init_installs_once_and_preserves_previous_command(
    setup, tmp_path, monkeypatch, has_previous,
):
    previous, _, _ = previous_statusline(tmp_path)
    original = {"env": {"KEEP": "untouched"}, "permissions": {"allow": ["Read"]}}
    if has_previous:
        original["statusLine"] = {"type": "command", "command": previous, "padding": 2}
    save_json(settings_path(), original)
    main, argv = init_cli(setup, monkeypatch)
    prompts = []
    monkeypatch.setattr("builtins.input", _skip_upstream(lambda prompt="": prompts.append(prompt) or "y"))
    assert cli_exit(main, argv) == 0
    assert len(prompts) == 1, "fa init must ask once before installing the Claude statusline wrapper"
    expected = {**original, "statusLine": {**original.get("statusLine", {}),
                                         "type": "command", "command": "fa statusline"}}
    assert json.loads(settings_path().read_text()) == expected
    _, record = data_paths()
    assert json.loads(record.read_text()) == {"enabled": True, "previous_command": previous if has_previous else None}
    unchanged = (settings_path().read_bytes(), record.read_bytes())

    def no_second_question(prompt=""):
        pytest.fail("Re-running init must not ask again or double wrap the user's command")

    monkeypatch.setattr("builtins.input", _skip_upstream(no_second_question))
    assert cli_exit(main, argv) == 0
    assert (settings_path().read_bytes(), record.read_bytes()) == unchanged


def test_statusline_wrapper_init_refusal_is_remembered_without_changing_claude_settings(
    setup, tmp_path, monkeypatch,
):
    previous, _, _ = previous_statusline(tmp_path)
    original = {"env": {"KEEP": "untouched"}, "statusLine": {"type": "command", "command": previous}}
    save_json(settings_path(), original)
    unchanged = settings_path().read_bytes()
    main, argv = init_cli(setup, monkeypatch)
    prompts = []
    monkeypatch.setattr("builtins.input", _skip_upstream(lambda prompt="": prompts.append(prompt) or "n"))
    assert cli_exit(main, argv) == 0
    assert len(prompts) == 1, "fa init must offer the optional wrapper before remembering a refusal"
    assert settings_path().read_bytes() == unchanged
    usage, record = data_paths()
    assert not usage.exists()
    assert json.loads(record.read_text()) == {"enabled": False, "previous_command": None}
    record_bytes = record.read_bytes()

    def no_second_question(prompt=""):
        pytest.fail("A declined wrapper must not be offered on the next init")

    monkeypatch.setattr("builtins.input", _skip_upstream(no_second_question))
    assert cli_exit(main, argv) == 0
    assert settings_path().read_bytes() == unchanged and record.read_bytes() == record_bytes


def test_statusline_wrapper_init_keeps_existing_install_and_does_not_double_wrap(
    setup, tmp_path, monkeypatch,
):
    previous, captured, calls = previous_statusline(tmp_path)
    install_record(previous)
    _, record = data_paths()
    unchanged = (settings_path().read_bytes(), record.read_bytes())
    main, argv = init_cli(setup, monkeypatch)

    def no_question(prompt=""):
        pytest.fail("An already installed wrapper needs no question")

    monkeypatch.setattr("builtins.input", _skip_upstream(no_question))
    assert cli_exit(main, argv) == 0
    assert (settings_path().read_bytes(), record.read_bytes()) == unchanged
    raw = statusline_input()
    result = run_statusline(tmp_path, raw)
    assert result.returncode == 0, "the installed wrapper must still call the original command after init"
    assert result.stdout == OUTPUT
    assert captured.read_bytes() == raw and calls.read_text() == "called\n"
