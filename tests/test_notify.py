import json
import subprocess

import pytest

from foragent import notify
from foragent.config import Config
from foragent.state import StateStore


@pytest.mark.parametrize("failure", [
    FileNotFoundError("missing executable"),
    subprocess.CalledProcessError(1, ["notifier"]),
    subprocess.TimeoutExpired(["notifier"], 10),
])
def test_process_failure_is_logged_and_returns_false(monkeypatch, caplog, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    assert notify.send(Config(notify="command:/notifier"), "Title", "Line") is False
    assert "Notification delivery failed" in caplog.text


def test_outbox_write_failure_is_isolated(tmp_path, monkeypatch, caplog):
    root = tmp_path / "state"
    root.write_text("not a directory")
    monkeypatch.setattr(StateStore, "for_repo", lambda repo: StateStore(root))
    assert notify.send(Config(repo="owner/repo"), "Title", "Line") is False
    assert caplog.records


def test_default_outbox_preserves_unicode_and_multiline_as_one_record(tmp_path, monkeypatch):
    monkeypatch.setattr(StateStore, "for_repo", lambda repo: StateStore(tmp_path))
    for _ in range(2):
        assert notify.send(Config(repo="owner/repo"), "검토", "첫 줄\n다음 줄") is True
    lines = (tmp_path / "push-outbox.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"title": "검토", "line": "첫 줄\n다음 줄", "link": None}


def test_command_runs_literal_path_and_clears_inherited_link(monkeypatch):
    calls = []
    monkeypatch.setenv("FA_LINK", "old link")
    monkeypatch.setenv("NOTIFY_TEST", "inherited")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert notify.send(Config(notify="command:/path with spaces/notify"), "Title", "Line")
    args, kwargs = calls[0]
    assert args == (["/path with spaces/notify"],)
    assert kwargs["env"]["FA_LINK"] == ""
    assert kwargs["env"]["NOTIFY_TEST"] == "inherited"
    assert kwargs["check"] is True and kwargs["timeout"] == 10


@pytest.mark.parametrize("target", ["unknown", "command:", "slack:", "ntfy:", {"old": "setting"}])
def test_invalid_destination_is_logged_without_raising(target, caplog):
    assert notify.send(Config(notify=target), "Title", "Line") is False
    assert caplog.records
