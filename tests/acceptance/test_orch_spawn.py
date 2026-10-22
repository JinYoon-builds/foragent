"""Acceptance test for orch-spawn. Locked by Foragent; do not edit."""

import json
from datetime import timedelta
from pathlib import Path
import shlex

from test_heartbeats import heartbeats_offline, heartbeats_write


def orch_spawn_case(git_history, tmp_path, monkeypatch, *, screen="background", seed_snapshot=True):
    import shutil
    import subprocess
    import sys

    from foragent import notify
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case, tick_context

    tmp_path.mkdir(parents=True, exist_ok=True)
    case = make_case(git_history, tmp_path, monkeypatch, language="ko")
    case.tracker.record_label(1, "approved", "human")
    # Import without spawning, even if an earlier case enabled it. The gate-
    # tick test defers the import until its queued/running setup is complete.
    if seed_snapshot:
        with monkeypatch.context() as seed:
            seed.setenv("FORAGENT_ORCH_SPAWN", "off")
            tick(tick_context(case, start_runs=False))
    commands, pushes = [], []
    real_run = subprocess.run
    real_popen = subprocess.Popen
    executable = tmp_path / "bin/orca"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)

    def record(argv, *args, **kwargs):
        argv = shlex.split(argv) if isinstance(argv, str) else [str(part) for part in argv]
        if Path(argv[0]).name == "git":
            return real_run(argv, *args, **kwargs)
        commands.append(argv)
        output = json.dumps({"handle": "term_orch"}) + "\n"
        text_mode = kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")
        return subprocess.CompletedProcess(argv, 0, stdout=output if text_mode else output.encode(),
                                           stderr="" if text_mode else b"")

    class OrchSpawnProcess:
        pid = 88001
        returncode = 0

        def __init__(self, argv, result):
            self.args, self.result = argv, result

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def poll(self):
            return self.returncode

        def wait(self, *args, **kwargs):
            return self.returncode

        def communicate(self, *args, **kwargs):
            return self.result.stdout, self.result.stderr

        def kill(self):
            self.returncode = -9

    def popen(argv, *args, **kwargs):
        if not isinstance(argv, str) and Path(argv[0]).name == "git":
            return real_popen(argv, *args, **kwargs)
        return OrchSpawnProcess(argv, record(argv, *args, **kwargs))

    def push(config, title, line, link=None):
        pushes.append({"title": title, "line": line, "link": link})
        return True

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(subprocess, "Popen", popen)
    # Also cover direct imports, without requiring a particular private runner name.
    module = sys.modules.get("foragent.orch_spawn")
    if module is not None:
        for name, fake in (("run", record), ("Popen", popen)):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, fake)
    monkeypatch.setattr(shutil, "which", lambda name: (
        str(executable) if name == "orca" and screen == "orca-path"
        else f"/offline/bin/{name}" if name in {"claude", "tmux"} else None))
    monkeypatch.setattr(notify, "send", push)
    monkeypatch.delenv("FORAGENT_ORCH_SPAWN", raising=False)
    monkeypatch.delenv("FORAGENT_VIEW", raising=False)
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    if screen == "orca-recorded":
        (case.store.root / "watch-terminal.json").write_text(json.dumps({
            "executable": str(executable), "handle": "term_existing",
        }))
    elif screen == "orca-path":
        monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_existing")
    elif screen == "tmux":
        monkeypatch.setenv("TMUX", "/offline/tmux,1,0")
    return case, commands, pushes


def orch_spawn_assert_push(pushes):
    assert len(pushes) == 1
    assert "오케스트레이터를 새로 열었어요" in pushes[0]["line"]


def test_orch_spawn_orca_uses_recorded_executable_or_path_and_opens_adjacent_view(
    git_history, tmp_path, monkeypatch,
):
    from foragent.orch_spawn import ensure_orchestrator

    for source in ("orca-recorded", "orca-path"):
        case, commands, pushes = orch_spawn_case(git_history, tmp_path / source, monkeypatch, screen=source)
        monkeypatch.setenv("TMUX", "/offline/tmux,1,0")  # Reachable Orca has priority over tmux.
        assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "orca"
        created = [argv for argv in commands if argv[1:3] == ["terminal", "create"]]
        assert len(created) == 1, commands
        argv = created[0]
        assert f"orch: {case.root.name}" in argv[argv.index("--title") + 1]
        command = shlex.split(argv[argv.index("--command") + 1])
        assert "claude" in [Path(part).name for part in command] and "/fa-orchestrator" in command
        splits = [argv for argv in commands if argv[1:3] == ["terminal", "split"]]
        assert len(splits) == 1, commands
        assert commands.index(created[0]) < commands.index(splits[0])
        assert splits[0][splits[0].index("--terminal") + 1] == "term_orch"
        assert splits[0][splits[0].index("--direction") + 1] == "vertical"
        view = splits[0][splits[0].index("--command") + 1]
        assert "gate watch" in view and str(case.root) in view
        assert not any(Path(argv[0]).name in {"tmux", "osascript"} for argv in commands)
        orch_spawn_assert_push(pushes)


def test_orch_spawn_tmux_opens_a_named_claude_window(git_history, tmp_path, monkeypatch):
    from foragent.orch_spawn import ensure_orchestrator

    case, commands, pushes = orch_spawn_case(git_history, tmp_path, monkeypatch, screen="tmux")
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "tmux"
    windows = [argv for argv in commands if Path(argv[0]).name == "tmux" and "new-window" in argv]
    assert len(windows) == 1, commands
    argv = windows[0]
    assert argv[argv.index("-n") + 1] == f"orch: {case.root.name}"
    command = " ".join(argv)
    assert "claude" in command and "/fa-orchestrator" in command
    assert not any(Path(argv[0]).name in {"orca", "osascript"} for argv in commands)
    orch_spawn_assert_push(pushes)


def test_orch_spawn_background_cooldown_survives_store_reopening(git_history, tmp_path, monkeypatch):
    from foragent.orch_spawn import ensure_orchestrator
    from foragent.state import StateStore

    case, commands, pushes = orch_spawn_case(git_history, tmp_path, monkeypatch)
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background"
    assert len(commands) == 1, commands
    assert [Path(commands[0][0]).name, *commands[0][1:]] == [
        "claude", "--bg", "-n", f"orch: {case.root.name}", "/fa-orchestrator",
    ]
    orch_spawn_assert_push(pushes)
    reopened = StateStore(case.store.root, now=lambda: case.clock[0])
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
    case.clock[0] += timedelta(seconds=119)
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) is None
    assert len(commands) == len(pushes) == 1
    case.clock[0] += timedelta(seconds=2)
    assert ensure_orchestrator(case.root, reopened, case.config, now=case.clock[0]) == "background"
    assert len(commands) == len(pushes) == 2


def test_orch_spawn_skips_fresh_heartbeat_disabled_spawn_and_no_approved_work(
    git_history, tmp_path, monkeypatch,
):
    from foragent.orch_spawn import ensure_orchestrator

    case, commands, pushes = orch_spawn_case(git_history, tmp_path, monkeypatch)
    path = heartbeats_write(case.store, "orch", case.clock[0] - timedelta(seconds=30))
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None
    assert commands == pushes == []
    path.unlink()
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None
    assert commands == pushes == []
    monkeypatch.delenv("FORAGENT_ORCH_SPAWN")
    case.store.finish_run(1, outcome="merged")
    case.store.clear_approval(1)
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) is None
    assert commands == pushes == []


def test_orch_spawn_gate_tick_opens_for_running_and_queued_approved_work(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    for state in ("running", "queued"):
        case, commands, pushes = orch_spawn_case(git_history, tmp_path / state, monkeypatch,
                                               seed_snapshot=False)
        if state == "queued":
            case.store.finish_run(1, outcome="not-started")
            case.tracker.remove_labels(1, ["running"])
            case.tracker.add_labels(1, ["ready"])
        tick(tick_context(case, start_runs=False))
        claude = [argv for argv in commands if Path(argv[0]).name == "claude"]
        assert len(claude) == 1 and "/fa-orchestrator" in claude[0], commands
        orch_spawn_assert_push([push for push in pushes if "오케스트레이터를 새로 열었어요" in push["line"]])
