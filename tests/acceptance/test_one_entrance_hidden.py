"""The real ensure command must use a background process even when GUI hosts exist."""

from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from test_one_entrance_seat import invoke, make_pm_case
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.mark.parametrize("host", ["orca", "tmux"])
def test_orchestrator_launch_never_opens_a_person_terminal(tmp_path, monkeypatch, capsys, isolated_home, host):
    case = make_pm_case(tmp_path, monkeypatch)
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.root / "worker", lane="api")
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "on")
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "offline-handle") if host == "orca" else monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.setenv("TMUX", "offline-tmux")
    monkeypatch.setattr(shutil, "which", lambda name: f"/offline/{name}" if name in {host, "claude"} else None)
    launches = []

    def external(argv, *args, **kwargs):
        launches.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def background(argv, *args, **kwargs):
        launches.append((list(argv), kwargs))
        return SimpleNamespace(pid=95001, poll=lambda: None)

    monkeypatch.setattr(subprocess, "run", external)
    monkeypatch.setattr(subprocess, "Popen", background)
    result = invoke(capsys, "orch", "ensure")
    assert result.code == 0, result.text
    assert launches, "A background orchestrator must actually start when work exists"
    visible = [argv for argv, _ in launches if Path(argv[0]).name in {"orca", "tmux", "osascript", "open", "wezterm", "kitty"}]
    assert visible == [], "The orchestrator must be hidden even when a terminal host is available: " + repr(visible)
    assert any(Path(argv[0]).name == "claude" or "foragent.cli" in argv for argv, _ in launches)
