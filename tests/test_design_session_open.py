"""Launch errors and context propagation for design sessions, without real windows."""

import json
from pathlib import Path
import shlex
import subprocess

import pytest

from foragent import cli, design_session
from foragent.config import Config
from foragent.state import StateStore


def terminal_environment(monkeypatch):
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "source")
    monkeypatch.delenv("ORCA_CLI_COMMAND", raising=False)
    monkeypatch.delenv("ORCA_DEV_REPO_ROOT", raising=False)
    monkeypatch.setattr(design_session.shutil, "which", lambda name: f"/offline/{name}")


def test_design_open_passes_literal_topic_agent_and_skill_without_github(tmp_path, monkeypatch, capsys):
    terminal_environment(monkeypatch)
    root = tmp_path / "project with spaces"
    agent = root / ".claude/agents/fa-design.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("---\nname: fa-design\n---\nUse the project style skills.\n")
    store = StateStore(tmp_path / "state")
    monkeypatch.setattr(cli, "load_config", lambda root: Config(repo="owner/project", language="ko"))
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo: store))

    def tracker_forbidden(*args, **kwargs):
        pytest.fail("Opening a design session must not initialize GitHub")

    monkeypatch.setattr(cli, "GhTracker", tracker_forbidden)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        handle = "design" if argv[2] == "create" else "preview"
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": {"handle": handle}}))

    monkeypatch.setattr(subprocess, "run", run)
    topic = "settings 'quoted' $(touch unwanted) `touch unwanted`\nnext line"
    assert cli.main(["design", "open", "--repo-root", str(root), "--topic", topic]) == 0
    command = shlex.split(calls[0][calls[0].index("--command") + 1])
    assert command[0] == "/offline/claude"
    assert command[command.index("--agent") + 1] == "fa-design"
    assert command[command.index("--append-system-prompt") + 1] == design_session._skill_source()
    assert json.dumps(topic, ensure_ascii=False) in command[-1]
    assert "(ko)" in command[-1]
    assert calls[0][calls[0].index("--worktree") + 1] == f"path:{root}"
    record_path, = (store.root / "design-sessions").glob("*.json")
    record = json.loads(record_path.read_text())
    assert (record["handle"], record["preview_handle"], record["topic"]) == ("design", "preview", topic)
    assert str(record_path) in command[-1]
    assert store.active_runs() == []
    assert not (root / "unwanted").exists()
    assert "Opened design: project with spaces" in capsys.readouterr().out


def test_failed_preview_keeps_live_design_tab_discoverable(tmp_path, monkeypatch, capsys):
    terminal_environment(monkeypatch)
    store = StateStore(tmp_path / "state")

    def run(argv, **kwargs):
        if argv[2] == "split":
            raise subprocess.CalledProcessError(1, argv, stderr="preview unavailable")
        return subprocess.CompletedProcess(argv, 0, stdout='{"handle":"live-design"}')

    monkeypatch.setattr(subprocess, "run", run)
    path = design_session.open_session(tmp_path, store)
    record = json.loads(path.read_text())
    assert record["handle"] == "live-design"
    assert record["preview_handle"] is None
    assert "preview unavailable" in record["preview_error"]
    assert store.active_runs() == []
    assert "design tab opened; preview pane failed" in capsys.readouterr().err


@pytest.mark.parametrize("error", [
    subprocess.CalledProcessError(1, ["orca", "terminal", "create"], stderr="cannot create tab"),
    subprocess.TimeoutExpired(["orca", "terminal", "create"], 10),
])
def test_failed_tab_launch_has_no_run_or_session_record(tmp_path, monkeypatch, error):
    terminal_environment(monkeypatch)
    store = StateStore(tmp_path / "state")

    def run(argv, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(OSError, match="orca terminal"):
        design_session.open_session(tmp_path, store)
    assert list(store.root.rglob("*.json")) == []
    assert store.active_runs() == []


def test_unavailable_terminal_reports_setup_without_starting_claude(tmp_path, monkeypatch, capsys):
    for key in ("ORCA_TERMINAL_HANDLE", "TMUX"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cli, "load_config", lambda root: Config(repo="owner/project"))
    monkeypatch.setattr(design_session.shutil, "which", lambda name: "/offline/claude" if name == "claude" else None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("No terminal is available"))
    assert cli.main(["design", "open", "--repo-root", str(tmp_path)]) == 1
    assert "Orca terminal or a tmux session" in capsys.readouterr().err


def test_installed_package_loads_its_bundled_skill(tmp_path, monkeypatch):
    prefix = tmp_path / "venv"
    installed = prefix / "share/foragent/skills/fa-design-session/SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("The installed design session instructions.\n")
    monkeypatch.setattr(design_session, "__file__", str(prefix / "lib/python3.12/site-packages/foragent/design_session.py"))
    monkeypatch.setattr(design_session.sys, "prefix", str(prefix))
    assert design_session._skill_source() == installed.read_text()
