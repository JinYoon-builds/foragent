"""Acceptance test for watch-reopen. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from test_gate_open_view import gate_case
from test_local_truth import record_gh
from test_skills_autosync import isolated_home
from test_view_open import assert_opened


def auto_opener(case, commands, monkeypatch, *, alive=False, timeout=False):
    """Reuse gate_case/CommandLog; fake process checks and the terminal _call boundary."""
    from foragent import watch_open

    state = SimpleNamespace(alive=alive, timeout=timeout, checks=[], attempts=[])
    for key in ("ORCA_TERMINAL_HANDLE", "TMUX", "WEZTERM_PANE", "KITTY_WINDOW_ID", "TERM_PROGRAM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("FORAGENT_VIEW", raising=False)
    executable = commands.path.parent / "bin" / "osascript"
    monkeypatch.setattr(watch_open.shutil, "which", lambda name: str(executable) if name == "osascript" else None)

    def running(store):
        state.checks.append(store)
        assert store is case.store, "Check the watch belonging to this repository"
        return state.alive

    def terminal(argv, root):
        state.attempts.append((list(argv), root))
        with commands.path.open("a") as stream:
            stream.write(json.dumps({"executable": Path(argv[0]).name, "argv": argv[1:], "cwd": str(root)}) + "\n")
        if state.timeout:
            state.timeout = False
            raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(watch_open, "_running", running)
    monkeypatch.setattr(watch_open, "_call", terminal)
    record_gh(monkeypatch)
    return state


def test_timeout_is_retried_on_next_tick_without_starting_another_run(
    git_history, tmp_path, monkeypatch, capsys, fake_process_table,
):
    from foragent.scheduler import tick

    case, _, commands, ctx, launched = gate_case(git_history, tmp_path, monkeypatch)
    opener = auto_opener(case, commands, monkeypatch, timeout=True)
    tick(ctx)
    assert launched == [1] and case.store.get_run(1).pid == 7001
    assert len(opener.attempts) == 1
    assert "terminal opener timed out" in capsys.readouterr().err
    commands.clear()
    fake_process_table.add(7001, cwd=case.root, start_time=case.clock[0].timestamp(),
                           argv=("fa", "gate", "run", "1"))

    case.clock[0] += timedelta(minutes=5)
    tick(ctx)
    assert launched == [1], "Recovery must not require a newly started issue"
    assert len(opener.attempts) == 2, (
        "The next gate tick must retry a timed-out opener when no watch is alive; "
        f"recorded {len(opener.attempts)} attempt(s)"
    )
    assert_opened(commands, case.root)
    assert capsys.readouterr().err == ""

    # A recovered watch suppresses further opens on subsequent ticks.
    opener.alive = True
    commands.clear()
    case.clock[0] += timedelta(minutes=5)
    tick(ctx)
    assert len(opener.attempts) == 2 and commands.calls() == []


def test_live_watch_opens_nothing_then_is_reopened_on_next_tick_after_it_dies(
    git_history, tmp_path, monkeypatch, fake_process_table,
):
    from foragent.scheduler import tick

    case, _, commands, ctx, launched = gate_case(git_history, tmp_path, monkeypatch)
    opener = auto_opener(case, commands, monkeypatch, alive=True)
    tick(ctx)
    assert launched == [1] and opener.checks, "Exercise a live-watch check while the first run starts"
    assert opener.attempts == [] and commands.calls() == [], "A live view must suppress all terminal opens"
    fake_process_table.add(7001, cwd=case.root, start_time=case.clock[0].timestamp(),
                           argv=("fa", "gate", "run", "1"))

    opener.alive = False
    case.clock[0] += timedelta(minutes=5)
    tick(ctx)
    assert launched == [1], "The original run is still active; no new run should trigger recovery"
    assert len(opener.attempts) == 1, (
        "The gate must reopen a vanished watch on the next tick even when no run starts"
    )
    assert_opened(commands, case.root)
    assert opener.checks[-1] is case.store
