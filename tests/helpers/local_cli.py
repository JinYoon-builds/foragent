"""Shared offline helpers for unit tests."""

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from helpers.github_transport import record_gh


def offline_local_cli(tmp_path, monkeypatch):
    """Real CLI/config/state, with user and machine files confined to this test."""
    from foragent import cli, executors, runner
    from foragent.state import StateStore

    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text("repo: owner/repo\nskill_libraries: []\n")
    state_home = tmp_path / "home/.local/state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    state_root = state_home / "foragent/owner__repo"
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kw: cls(state_root, **kw)))
    monkeypatch.chdir(root)

    def no_executor(*args, **kwargs):
        pytest.fail("Local progress, notes, settings and tidy must not call an executor/LLM")

    monkeypatch.setattr(executors, "executor_for", no_executor)
    monkeypatch.setattr(runner, "executor_for", no_executor)
    monkeypatch.setattr(executors.CodexExecutor, "run", no_executor)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", no_executor)
    run = subprocess.run

    def local_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv:
            assert Path(argv[0]).name not in {
                "codex", "claude", "orca", "tmux", "osascript", "open", "wezterm", "kitty",
            }, f"Local facts must not start an LLM or control a real terminal: {argv}"
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", local_run)
    calls = record_gh(monkeypatch)
    return SimpleNamespace(main=cli.main, root=root, store=StateStore(state_root),
                           state_home=state_home, gh_calls=calls)
