import json
import sys

from foragent.config import Config
from foragent.executors import codex_error_text
from foragent.runner import live_check


def _fake(bin_dir, name, body):
    script = bin_dir / name
    script.write_text(f"#!{sys.executable}\nimport json, sys\nsys.stdin.read()\n{body}\n")
    script.chmod(0o755)


def test_committed_smoke_file_does_not_pass_an_idle_executor(git_history, tmp_path, monkeypatch):
    git_history.update({"hello.txt": "ok\n"})
    repo = git_history.origin
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "claude", "print(json.dumps({'type': 'result', 'num_turns': 3, 'result': 'Done'}))")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    check = live_check(repo, Config(executor="claude"), "claude", timeout_s=60)
    assert not check.passed and "hello.txt" in check.line()


def test_codex_error_only_run_quotes_its_error_text(git_history, tmp_path, monkeypatch):
    repo = git_history.origin
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "codex", "print(json.dumps({'type': 'thread.started', 'thread_id': 't'}))\n"
                            "print(json.dumps({'type': 'error', 'message': 'Model not supported here'}))\n"
                            "sys.exit(1)")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    check = live_check(repo, Config(executor="codex"), "codex", timeout_s=60)
    assert not check.passed and "Model not supported here" in check.line()


def test_codex_error_text_reads_error_and_failed_turn_events(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text("\n".join(json.dumps(event) for event in [
        {"type": "turn.completed", "message": "ignored"},
        {"type": "error", "message": "first"},
        {"type": "turn.failed", "error": {"message": "second"}},
    ]) + "\n")
    assert codex_error_text(events) == "first\nsecond"
