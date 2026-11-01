"""The Stop hook's slot wait and interruptions block completion (#993)."""

import fcntl
import json
import time
from types import SimpleNamespace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))

from foragent import capacity, run_check_slot, run_checks, runner
from test_orch_review_handoff import make_case
from test_board_status_groups import offline_github  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


def _case(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true", test_slots=1)
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    return case, case.store.root / "approvals/1.json"


def _hook_timeout(snapshot, seconds):
    """Write the Stop hook timeout the way run_execution writes it for this run."""
    directory = snapshot.parent.parent / "runs" / f"issue-{snapshot.stem}"
    directory.mkdir(parents=True, exist_ok=True)
    hooks = {"Stop": [{"hooks": [{"type": "command", "command": "check-done", "timeout": seconds}]}]}
    (directory / run_check_slot.SETTINGS_FILE).write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def test_commands_together_fit_inside_the_hook_timeout(git_history, tmp_path, monkeypatch):
    case, snapshot = _case(git_history, tmp_path, monkeypatch)
    _hook_timeout(snapshot, run_check_slot.MARGIN_SECONDS + 4)
    timeouts = []

    def slow(argv, **kw):
        timeouts.append(kw["timeout_s"])
        time.sleep(2)  # The first command uses half of what the hook has.
        return 0, False, ""

    monkeypatch.setattr(runner, "run_logged", slow)
    assert run_checks.check_done(snapshot, case.root, case.config.acceptance_dir) == 0
    assert len(timeouts) == 2 and timeouts[0] <= 4 and timeouts[1] <= 2, timeouts


def test_no_time_left_blocks_the_stop_without_starting_the_next_command(git_history, tmp_path, monkeypatch, capsys):
    case, snapshot = _case(git_history, tmp_path, monkeypatch)
    _hook_timeout(snapshot, run_check_slot.MARGIN_SECONDS + 2)
    calls = []

    def slow(argv, **kw):
        calls.append(argv)
        time.sleep(2.2)  # The first command uses all the time the hook has.
        return 0, False, ""

    monkeypatch.setattr(runner, "run_logged", slow)
    assert run_checks.check_done(snapshot, case.root, case.config.acceptance_dir) == 2
    assert len(calls) == 1
    assert "no time left" in capsys.readouterr().err


def test_hook_settings_and_budget_share_one_timeout():
    config = SimpleNamespace(max_runtime_minutes=15)
    assert run_check_slot.hook_seconds(config) == 900
    assert run_check_slot.hook_seconds(SimpleNamespace(max_runtime_minutes=90)) == 5400


def test_slot_wait_past_the_hook_deadline_blocks_the_stop(git_history, tmp_path, monkeypatch, capsys):
    case, snapshot = _case(git_history, tmp_path, monkeypatch)
    _hook_timeout(snapshot, run_check_slot.MARGIN_SECONDS + 1)  # The slot wait gets about one second.
    slots = capacity.machine_root() / capacity.TEST_SLOTS_DIR
    slots.mkdir(parents=True, exist_ok=True)
    taken = [(slots / f"slot-{k}.lock").open("a") for k in range(4)]
    for stream in taken:  # Another process holds every slot.
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    calls = []
    monkeypatch.setattr(runner, "run_logged", lambda argv, **kw: calls.append(argv) or (0, False, ""))
    try:
        assert run_checks.check_done(snapshot, case.root, case.config.acceptance_dir) == 2
    finally:
        for stream in taken:
            stream.close()
    assert not calls
    assert "Stop hook's deadline" in capsys.readouterr().err


def test_interrupted_check_blocks_the_stop(git_history, tmp_path, monkeypatch, capsys):
    case, snapshot = _case(git_history, tmp_path, monkeypatch)

    def interrupted(argv, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "run_logged", interrupted)
    assert run_checks.check_done(snapshot, case.root, case.config.acceptance_dir) == 2
    assert "KeyboardInterrupt" in capsys.readouterr().err
