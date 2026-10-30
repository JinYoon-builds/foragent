"""Acceptance test for orca-handle. Locked by Foragent; do not edit."""

from datetime import datetime
from io import BytesIO, TextIOWrapper
import json
from pathlib import Path
import shlex
import shutil
import sys
from types import SimpleNamespace

import pytest
import yaml

from test_fa_report_since import contains_time
from test_heartbeats import heartbeats_offline  # noqa: F401 -- offline autouse fixture
from test_orch_spawn import orch_spawn_case
from test_skills_autosync import cli_exit


@pytest.fixture
def handle_case(git_history, tmp_path, monkeypatch):
    from foragent import cli, presence, statusline
    from foragent.state import StateStore

    real_which = shutil.which
    case, commands, pushes = orch_spawn_case(
        git_history, tmp_path, monkeypatch, seed_snapshot=False,
    )
    (case.root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": case.config.repo, "language": "ko", "skill_libraries": [],
    }), encoding="utf-8")
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: case.store))
    executable = (tmp_path / "bin/orca").resolve()
    monkeypatch.setenv("PATH", str(executable.parent))
    fallback_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: (
        real_which(name) if name == "orca" else fallback_which(name)
    ))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.clock[0].astimezone(tz) if tz else case.clock[0].replace(tzinfo=None)

    monkeypatch.setattr(statusline, "datetime", Clock)
    monkeypatch.setattr(presence, "datetime", Clock)
    record = case.store.root / "watch-terminal.json"
    record.write_bytes(b'{\n  "handle": "term_old", "executable": "/old/orca"\n}\n')

    def hook():
        # This is the command the real statusline installer puts in SessionStart.
        command = shlex.split(statusline.HOOK_COMMAND)
        assert Path(command[0]).name in {"fa", "foragent"}
        payload = {"hook_event_name": "SessionStart", "source": "startup",
                   "cwd": str(case.root), "session_id": "offline-planning-session"}
        stdin = TextIOWrapper(BytesIO(json.dumps(payload).encode()), encoding="utf-8")
        stdout, stderr = [TextIOWrapper(BytesIO(), encoding="utf-8") for _ in range(2)]
        with monkeypatch.context() as streams:
            streams.setattr(sys, "stdin", stdin)
            streams.setattr(sys, "stdout", stdout)
            streams.setattr(sys, "stderr", stderr)
            code = cli_exit(cli.main, command[1:])
            stdout.flush()
            stderr.flush()
            output, error = stdout.buffer.getvalue(), stderr.buffer.getvalue()
        assert code == 0, f"The installed SessionStart hook must succeed: {error!r}"
        assert error == b"", "Recording a terminal must not add hook stderr"
        return output

    return SimpleNamespace(case=case, commands=commands, pushes=pushes, executable=executable,
                           record=record, hook=hook)


def test_session_start_replaces_stale_handle_and_existing_readers_use_it(handle_case, monkeypatch):
    from foragent.orch_spawn import ensure_orchestrator
    from foragent.watch_open import reopen

    local = handle_case
    case = local.case
    before = local.record.read_bytes()
    without_handle = local.hook()
    assert json.loads(without_handle)["systemMessage"], "Compare real, nonempty hook output"
    assert local.record.read_bytes() == before
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_new")
    assert local.hook() == without_handle, "Recording the live handle must preserve stdout byte for byte"
    saved = json.loads(local.record.read_bytes())
    assert saved.get("handle") == "term_new", "SessionStart must replace the stale Orca handle"
    assert saved.get("executable") == str(local.executable), "Store the absolute executable found on PATH"
    assert "recorded_at" in saved and contains_time(
        json.dumps(saved["recorded_at"]).encode(), case.clock[0],
    ), "Record when this planning session supplied its live terminal"
    assert local.commands == local.pushes == [], "Recording alone must launch nothing and send no push"
    recorded = local.record.read_bytes()

    # The gate has neither the planning handle nor Orca on its own PATH.
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE")
    monkeypatch.setenv("PATH", str(case.root / "no-gate-tools"))
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "orca"
    shown = [argv for argv in local.commands if argv[1:3] == ["terminal", "show"]]
    assert shown == [[str(local.executable), "terminal", "show", "--terminal", "term_new"]]
    created = [argv for argv in local.commands if argv[1:3] == ["terminal", "create"]]
    assert len(created) == 1, local.commands
    assert created[0][0] == str(local.executable)
    assert created[0][created[0].index("--worktree") + 1] == f"path:{case.root.resolve()}"
    assert created[0][created[0].index("--title") + 1] == f"orch: {case.root.name}"
    assert "/fa-orchestrator" in shlex.split(created[0][created[0].index("--command") + 1])
    assert not any(Path(argv[0]).name in {"osascript", "tmux", "claude"} for argv in local.commands)
    assert not any(argv[1:3] == ["terminal", "split"] for argv in local.commands), (
        "Keep the existing D76 orchestrator tab; only the lane view splits"
    )
    assert not any("term_old" in argv for argv in local.commands)
    assert local.record.read_bytes() == recorded

    local.commands.clear()
    reopen(case.root, case.store, case.config.language, case.clock[0])
    splits = [argv for argv in local.commands if argv[1:3] == ["terminal", "split"]]
    assert len(splits) == 1 and splits[0][:5] == [
        str(local.executable), "terminal", "split", "--terminal", "term_new",
    ], local.commands
    assert not any(Path(argv[0]).name == "osascript" for argv in local.commands)
    assert local.record.read_bytes() == recorded, "Gate readers must retain the planning session's record"


@pytest.mark.parametrize("missing", ["handle", "orca"])
def test_session_start_missing_terminal_context_leaves_record_byte_identical(handle_case, monkeypatch, missing):
    local = handle_case
    before = local.record.read_bytes()
    baseline = local.hook()
    if missing == "orca":
        monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_new")
        monkeypatch.setenv("PATH", str(local.case.root / "no-orca"))
    assert local.hook() == baseline
    assert local.record.read_bytes() == before, "Missing terminal context must leave the old record untouched"
    local.record.unlink()
    assert local.hook() == baseline
    assert not local.record.exists(), "Missing terminal context must not create a terminal record"


def test_session_start_unwritable_state_keeps_success_and_identical_stdout(handle_case, monkeypatch):
    import builtins
    import io
    import os

    local = handle_case
    baseline = local.hook()
    before = local.record.read_bytes()
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_new")
    state_root = local.case.store.root.resolve()

    def in_state(path):
        return isinstance(path, (str, os.PathLike)) and Path(path).absolute().is_relative_to(state_root)

    def deny_writes(opener):
        def open_file(path, mode="r", *args, **kwargs):
            if in_state(path) and any(flag in mode for flag in "wax+"):
                raise PermissionError("offline unwritable state directory")
            return opener(path, mode, *args, **kwargs)
        return open_file

    real_open = os.open

    def open_fd(path, flags, *args, **kwargs):
        if in_state(path) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise PermissionError("offline unwritable state directory")
        return real_open(path, flags, *args, **kwargs)

    # Deny filesystem writes, including atomic temporary files, even as root.
    monkeypatch.setattr(builtins, "open", deny_writes(builtins.open))
    monkeypatch.setattr(io, "open", deny_writes(io.open))
    monkeypatch.setattr(os, "open", open_fd)
    assert local.hook() == baseline, "An unwritable terminal record must not change the hook's summary"
    assert local.record.read_bytes() == before
