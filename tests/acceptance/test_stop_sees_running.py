"""Acceptance test for stop-sees-running. Locked by Foragent; do not edit."""

import json

import pytest

from test_keep_unfinished_work import _assert_saved
from test_orch_stop import _case, _snapshot
from test_skills_autosync import cli_exit
from test_stop_whole_tree import events, fake_processes


WHY = "Emergency halt while keeping the unfinished work"


def gate_status(main, capsys):
    capsys.readouterr()
    code = cli_exit(main, ["gate", "status", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    return json.loads(captured.out)


def stop(main, capsys):
    capsys.readouterr()
    code = cli_exit(main, ["orch", "stop", "1", "--keep-work", "--why", WHY])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


@pytest.mark.parametrize("worker", ["alive", "gone"])
def test_stop_accepts_the_implementing_record_status_displays_and_keeps_work(
    git_history, tmp_path, monkeypatch, fake_processes, capsys, worker,
):
    case, tree, main = _case(git_history, tmp_path, monkeypatch, fake_processes)
    case.store.update_run(1, phase="implementing")
    if worker == "gone":
        # The incident: the record is unfinished, but its recorded processes are gone.
        for row in tree:
            fake_processes.exit(row.pid)
    protected = fake_processes.live - {row.pid for row in tree}
    displayed = gate_status(main, capsys)
    assert [(row["number"], row["status"], row["phase"]) for row in displayed["active_runs"]] == [
        (1, "running", "implementing"),
    ], displayed

    code, message = stop(main, capsys)
    assert code == 0, f"Stop refused the implementing run shown by status ({worker}): {message}"
    run = case.store.get_run(1)
    assert (run.status, run.outcome) == ("finished", "stopped"), run
    assert fake_processes.live == protected, "Stop left owned workers alive or touched another process"
    _assert_saved(case, WHY)  # Inspect saved files and the issue branch through offline Git.
    assert not case.tracker.get_issue(1).labels & {"running", "blocked", "needs-human"}
    assert gate_status(main, capsys)["active_runs"] == []
    assert len([run for run in case.store.read_history() if run.number == 1]) == 1
    assert len([event for event in events(case) if event["kind"] == "stop" and event["issue"] == 1]) == 1


def test_status_and_stop_agree_a_finished_record_is_not_running(
    git_history, tmp_path, monkeypatch, fake_processes, capsys,
):
    case, _, main = _case(git_history, tmp_path, monkeypatch, fake_processes)
    case.store.update_run(1, phase="implementing")
    case.store.finish_run(1, outcome="merged")
    assert gate_status(main, capsys)["active_runs"] == []
    before, alive = _snapshot(case), set(fake_processes.live)
    code, message = stop(main, capsys)
    assert code != 0 and "not running" in message.lower(), message
    assert _snapshot(case) == before, "Refusing a finished record changed its work or state"
    assert fake_processes.live == alive and fake_processes.signals == []
