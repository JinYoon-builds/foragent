from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from foragent import kick
from foragent.config import Config, ConfigError
from foragent.scheduler import tick
from foragent.state import StateStore


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "repo with spaces"
    root.mkdir()
    store = StateStore(tmp_path / "state")
    repos, remotes = [], []
    monkeypatch.setattr(kick, "load_config", lambda root: Config(repo="custom/project"))
    monkeypatch.setattr(kick, "GitRepo", lambda root: SimpleNamespace(
        remote_slug=lambda: remotes.append(root) or "owner/repo",
    ))
    monkeypatch.setattr(kick.StateStore, "for_repo", lambda repo: repos.append(repo) or store)
    return root, store, repos, remotes


@pytest.mark.parametrize("configured_repo", ["custom/project", None])
def test_kick_detaches_same_python_and_appends_both_streams(environment, monkeypatch, configured_repo):
    root, store, repos, remotes = environment
    monkeypatch.setattr(kick, "load_config", lambda root: Config(repo=configured_repo))
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs["stdout"] is kwargs["stderr"]
        kwargs["stdout"].write(b"tick output\n")
        return object()  # No wait, communicate, or context manager.

    monkeypatch.setattr(kick.subprocess, "Popen", popen)
    monkeypatch.chdir(root.parent)
    assert kick.kick_tick(Path(root.name)) is None
    assert kick.kick_tick(root) is None
    assert calls[0][0] == [sys.executable, "-m", "foragent.cli", "gate", "tick", "--repo-root", str(root)]
    assert set(calls[0][1]) == {"cwd", "stdin", "start_new_session", "stdout", "stderr"}
    assert calls[0][1]["cwd"] == root
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    assert calls[0][1]["start_new_session"] is True
    assert all(kwargs["stdout"].closed for _, kwargs in calls)
    assert (store.root / "kick.log").read_text() == "tick output\ntick output\n"
    assert repos == [configured_repo or "owner/repo"] * 2
    assert remotes == ([] if configured_repo else [root, root])


@pytest.mark.parametrize("error", [OSError("spawn failed\nmore details"), RuntimeError("spawn failed")])
def test_launch_failure_is_logged_and_ignored(environment, monkeypatch, capsys, error):
    root, store, _, _ = environment
    store.root.mkdir()
    log = store.root / "kick.log"
    log.write_text("previous tick\n")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(kick.subprocess, "Popen", fail)
    assert kick.kick_tick(root) is None
    assert log.read_text() == f"previous tick\nforagent: tick kick failed: {' '.join(str(error).split())}\n"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("failure", ["config", "state-directory"])
def test_setup_failure_is_only_logged(environment, monkeypatch, caplog, failure):
    root, store, _, _ = environment
    if failure == "config":
        def invalid(root):
            raise ConfigError("invalid configuration")

        monkeypatch.setattr(kick, "load_config", invalid)
    else:
        store.root.write_text("a file blocks the state directory")
    calls = []
    monkeypatch.setattr(kick.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))
    assert kick.kick_tick(root) is None
    assert calls == []
    assert "Could not kick tick" in caplog.text


def test_kick_while_tick_lock_is_held_safely_skips(environment, monkeypatch):
    root, store, _, _ = environment
    reports = []

    def popen(args, **kwargs):
        # Execute the injected child inline while the original tick owns the lock.
        # No tracker or Git adapter is available for a skipped tick to touch.
        report = tick(SimpleNamespace(store=store))
        reports.append(report)
        kwargs["stdout"].write((report.skipped + "\n").encode())
        return object()

    monkeypatch.setattr(kick.subprocess, "Popen", popen)
    with store.tick_lock() as acquired:
        assert acquired
        kick.kick_tick(root)
    assert len(reports) == 1
    assert reports[0].skipped == "another tick is running"
    assert reports[0].actions == []
    assert (store.root / "kick.log").read_text() == "another tick is running\n"
    with store.tick_lock() as acquired:
        assert acquired
