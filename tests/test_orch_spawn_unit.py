"""Boundaries of the gate's orchestrator spawn (D76) beyond the acceptance file."""

from datetime import UTC, datetime, timedelta
import shutil
import subprocess

from foragent import notify, orch_spawn
from foragent.config import Config
from foragent.state import StateStore

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


def _setup(tmp_path, monkeypatch, *, claude=True, fail=False):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.save_approval(1, body="b", body_sha="s", acceptance_blobs={}, approved_by="human",
                        approved_at=NOW.isoformat())
    calls, pushes = [], []

    def run(argv, *args, **kwargs):
        calls.append(argv)
        if fail:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.delenv("FORAGENT_ORCH_SPAWN", raising=False)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(shutil, "which", lambda name: f"/offline/bin/{name}" if claude and name == "claude" else None)
    monkeypatch.setattr(notify, "send", lambda config, title, line, link=None: pushes.append(line) or True)
    repo = tmp_path / "repo"
    repo.mkdir()
    return store, repo, calls, pushes


def test_failed_spawn_reserves_spacing_without_a_push(tmp_path, monkeypatch):
    store, repo, calls, pushes = _setup(tmp_path, monkeypatch, fail=True)
    config = Config(repo="owner/repo", language="en")
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW) is None
    assert len(calls) == 1 and pushes == []
    assert orch_spawn.ensure_orchestrator(repo, store, config, now=NOW + timedelta(seconds=60)) is None
    assert len(calls) == 1


def test_missing_claude_opens_nothing_and_keeps_no_record(tmp_path, monkeypatch):
    store, repo, calls, pushes = _setup(tmp_path, monkeypatch, claude=False)
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="owner/repo"), now=NOW) is None
    assert calls == pushes == []
    assert not (store.root / "orch-spawn.json").exists()


def test_stale_heartbeat_spawns_and_english_push_names_the_repo(tmp_path, monkeypatch):
    store, repo, _calls, pushes = _setup(tmp_path, monkeypatch)
    store.write_heartbeat("orchestrator", NOW - timedelta(seconds=31))
    assert orch_spawn.ensure_orchestrator(repo, store, Config(repo="owner/repo"), now=NOW) == "background"
    assert pushes == ["Opened a new orchestrator for owner/repo (background)."]


def test_tick_hook_reports_through_the_messages_registry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    report = SimpleNamespace(actions=[])
    ctx = SimpleNamespace(repo_root=tmp_path, store=None, config=Config(repo="owner/repo", language="ko"),
                          now=lambda: NOW)

    def broken(*args, **kwargs):
        raise OSError("disk\nfull")

    monkeypatch.setattr(orch_spawn, "ensure_orchestrator", broken)
    assert orch_spawn.on_tick(ctx, report) is report
    monkeypatch.setattr(orch_spawn, "ensure_orchestrator", lambda *args, **kwargs: "orca")
    orch_spawn.on_tick(ctx, report)
    assert report.actions == ["오케스트레이터를 열지 못했어요: disk full", "오케스트레이터를 열었어요 (orca)"]


def test_tick_hook_never_raises_on_an_incomplete_context():
    from types import SimpleNamespace

    report = SimpleNamespace(actions=[])
    assert orch_spawn.on_tick(SimpleNamespace(), report) is report
    assert len(report.actions) == 1 and report.actions[0].startswith("Could not open an orchestrator:")
