"""Locked acceptance: one FA command replaces a stale dream without losing notebooks.

Break-it: reject closing the wrong PR, closing twice, skipping/repeating the new
dream, or clearing either curated memory or pending lessons.
"""

from datetime import UTC, datetime
from pathlib import Path
import socket
import subprocess

import pytest
import yaml

from foragent import capacity, cli, gh, launchd, orch_dream, processes, scheduler
from foragent.config import load_config
from foragent.git import GitRepo
from foragent.state import StateStore
from foragent.tracker import FakeTracker


@pytest.mark.parametrize("lane", ["alpha", "beta"])
def test_redo_closes_only_requested_dream_and_starts_once(tmp_path, monkeypatch, capsys, lane):
    home, root = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(home / key.lower()))
    for key in ("FORAGENT_VIEW", "FORAGENT_ORCH_SPAWN", "FORAGENT_UPDATE_CHECK", "FORAGENT_CPU_BUSY"):
        monkeypatch.setenv(key, "off")
    for key in ("FORAGENT_SESSION_ID", "FORAGENT_ROLE"):
        monkeypatch.delenv(key, raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail(f"Dream redo must use fake services: {args}")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(processes, "list_processes", lambda: processes.ProcessTable([]))
    monkeypatch.setattr(launchd, "refresh_from_tick", lambda *args: None)
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.0))
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path / "machine")
    monkeypatch.setattr(capacity, "slept_seconds", lambda *args: 0.0)
    now = datetime(2026, 10, 31, 9, tzinfo=UTC)
    (root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "example/project", "checks": "ci", "skill_libraries": [],
        "lanes": {name: {"paths": [f"{name}/**"], "agent": f"fa-{name}"}
                  for name in ("alpha", "beta")},
    }), encoding="utf-8")
    config = load_config(root)
    store = StateStore(tmp_path / "state", now=lambda: now)
    tracker = FakeTracker(now=lambda: now)
    prs = {name: tracker.create_pr(head=f"foragent/dream-{name}", base="main",
                                  title=f"Dream {name}", body="Old proposal")
           for name in ("alpha", "beta")}
    unrelated = tracker.create_pr(head="foragent/issue-17", base="main", title="Work", body="Keep")
    notebooks = {}
    for name in ("alpha", "beta"):
        directory = root / f".claude/agent-memory/fa-{name}"
        directory.mkdir(parents=True)
        for filename in ("MEMORY.md", "MEMORY.new.md", "notes.md"):
            path = directory / filename
            path.write_text(f"Keep {name} {filename}.\n", encoding="utf-8")
            notebooks[path] = path.read_bytes()
    # A real completed issue makes this lane normally due; the stale PR blocks it.
    store.start_run(17, lane=lane, branch="foragent/issue-17", worktree=root / "finished")
    store.finish_run(17, "merged")
    closed, starts = [], []
    original_close = tracker.close_pr

    def close(number, comment=None):
        closed.append(number)
        return original_close(number, comment)

    monkeypatch.setattr(tracker, "close_pr", close)
    git = GitRepo(root, lambda args: "" if args[0] == "fetch" else forbidden(args))
    monkeypatch.setattr(cli, "GhTracker", lambda *args, **kwargs: tracker)
    monkeypatch.setattr(cli, "GitRepo", lambda *args, **kwargs: git)
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, *args, **kwargs: store))
    monkeypatch.setattr(cli, "kick_tick", lambda *args, **kwargs: None)

    def launch(*args, **kwargs):
        assert tracker.get_issue(prs[lane]).state == "closed", "Close the stale PR before starting fresh"
        starts.append(kwargs)
        assert kwargs == {"kind": "dream", "lane": lane}
        assert all(path.read_bytes() == data for path, data in notebooks.items())
        store.finish_run(f"dream-{lane}", "unchanged")
        orch_dream.record_done(store, lane, 0, "en", now)
        return None

    ctx = scheduler.TickContext(repo="example/project", repo_root=root, tracker=tracker, store=store,
                                config=config, git=git, now=lambda: now, launcher=launch,
                                runner_process_exists=lambda number: False, kill_runner=forbidden,
                                baseline_launcher=forbidden)
    gh.clear_recorder()
    try:
        scheduler.tick(ctx)
        assert starts == [], "The stale PR must block ordinary dreaming before redo"
        # Public contract chosen for this order; no production helper is replaced.
        try:
            result = cli.main(["orch", "dream", "--redo", lane, "--repo-root", str(root)])
        except SystemExit as exc:
            result = exc.code
        output = capsys.readouterr()
        assert result == 0, "fa orch dream --redo must be available: " + output.err + output.out
        scheduler.tick(ctx)
        assert closed == [prs[lane]], "Exactly the requested lane's stale PR must close once"
        assert starts == [{"kind": "dream", "lane": lane}], "Redo must reach the dream launcher once"
        assert tracker.get_issue(prs[lane]).state == "closed"
        other = "beta" if lane == "alpha" else "alpha"
        assert tracker.get_issue(prs[other]).state == "open"
        assert tracker.get_issue(unrelated).state == "open"
        assert all(path.read_bytes() == data for path, data in notebooks.items())
        scheduler.tick(ctx)
        assert closed == [prs[lane]] and starts == [{"kind": "dream", "lane": lane}], (
            "A later tick must not repeat the closure or the completed fresh dream"
        )
    finally:
        gh.clear_recorder()
