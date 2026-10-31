"""The Stop hook's slot wait and interruptions block completion (#993)."""

import fcntl
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


def test_slot_wait_past_the_hook_deadline_blocks_the_stop(git_history, tmp_path, monkeypatch, capsys):
    case, snapshot = _case(git_history, tmp_path, monkeypatch)
    # Every Done when command's execution time fills the hook: the slot wait gets one try.
    monkeypatch.setattr(run_check_slot, "COMMAND_SECONDS", case.config.max_runtime_minutes * 60)
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
