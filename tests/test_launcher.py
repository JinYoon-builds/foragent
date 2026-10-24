import sys
import subprocess

from foragent.launcher import spawn_notify, spawn_runner
from foragent.state import StateStore


def test_launcher_detaches_appends_both_streams_and_returns_pid(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    store.start_run(7, branch="branch", worktree=tmp_path)
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


def test_runner_birth_is_persisted_before_launcher_returns_a_reusable_pid(tmp_path, monkeypatch, fake_process_table):
    from foragent import processes

    table = fake_process_table
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree=tmp_path)

    class Process:
        pid = 42000

        def __init__(self, *args, **kwargs):
            table.add(self.pid, cwd=tmp_path, start_time=10)

        def __del__(self):
            # The launcher's reference ends only after persisting birth evidence.
            table.exit(self.pid)
            table.add(self.pid, cwd=tmp_path, start_time=20)

    monkeypatch.setattr("foragent.launcher.subprocess.Popen", Process)
    pid = spawn_runner(tmp_path, store)(1)
    assert store.get_run(1).process_identities == [{"pid": pid, "start_time": 10}]
    assert table.rows[pid].start_time == 20
    run = store.update_run(1, pid=pid)
    with processes.recorded_tree(run.process_identities):
        assert processes.kill_process_group(pid) == 0
    assert pid in table.live and not table.calls


def test_runner_without_readable_birth_leaves_stop_refused(tmp_path, monkeypatch, fake_process_table):
    import pytest
    from foragent import processes

    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="branch", worktree=tmp_path)

    class Process:
        pid = 42000

    def denied(*args, **kwargs):
        raise PermissionError("birth unavailable")

    monkeypatch.setattr("foragent.launcher.subprocess.Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr("foragent.launcher.observe_tree", denied)
    pid = spawn_runner(tmp_path, store)(1)
    run = store.get_run(1)
    assert run.pid == pid and run.process_identities == []
    with processes.recorded_tree(run.process_identities), pytest.raises(ValueError, match="No recorded process identity"):
        processes.kill_process_group(pid)
    assert not fake_process_table.calls
