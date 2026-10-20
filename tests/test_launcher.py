import sys
import subprocess

from foragent.launcher import spawn_notify, spawn_runner
from foragent.state import StateStore


def test_launcher_detaches_appends_both_streams_and_returns_pid(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    root = tmp_path / "repo with spaces"
    calls = []

    class Process:
        pid = 4321

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs["stdout"] is kwargs["stderr"]
        assert kwargs["start_new_session"] is True
        kwargs["stdout"].write(b"runner output\n")
        return Process()

    monkeypatch.setattr("foragent.launcher.subprocess.Popen", popen)
    launch = spawn_runner(root, store)
    assert launch(7) == 4321
    assert launch(7) == 4321
    assert calls[0][0] == [
        sys.executable, "-m", "foragent.cli", "gate", "run-issue", "7", "--repo-root", str(root),
    ]
    assert set(calls[0][1]) == {"start_new_session", "stdout", "stderr"}
    assert calls[0][1]["stdout"].closed
    assert store.log_path(7).read_text() == "runner output\nrunner output\n"


def test_notification_detaches_without_waiting_and_appends_both_streams(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    root = tmp_path / "repo with spaces"
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs["stdout"] is kwargs["stderr"]
        kwargs["stdout"].write(b"notification output\n")
        # No wait, communicate, or context manager is available on this child.
        return object()

    monkeypatch.setattr("foragent.launcher.subprocess.Popen", popen)
    assert spawn_notify(root, store) is None
    assert spawn_notify(root, store) is None
    assert calls[0][0] == [
        sys.executable, "-m", "foragent.cli", "inspect", "notify", "--repo-root", str(root),
    ]
    assert set(calls[0][1]) == {"stdin", "start_new_session", "stdout", "stderr"}
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["stdout"].closed
    assert (store.root / "notify.log").read_text() == "notification output\nnotification output\n"
