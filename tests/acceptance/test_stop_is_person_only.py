"""Locked acceptance: `fa stop` is a person's command (architect r769).

On 2026-11-05 04:09Z a role session's double-quoted free text ran `fa stop` through shell
command substitution and paused every lane of the live project. `fa stop` joins the #1736
person-only commands: inside a session Foragent launched it refuses with the same one-line
person-only refusal those commands give and changes nothing. The person's terminal and the
orchestrator (its existing exempt marker) keep it. Each case first proves both still stop every lane, then the refusal.

Sessions are captured exactly as test_person_only_commands.py does: launched through
Foragent's own launch paths with an offline model stand-in. Not pinned: the refusal wording
beyond "the same line the other person-only commands print, naming this command".
"""

import json

import pytest

from foragent import capacity
from test_order_progress import offline_local_cli
from test_person_only_commands import SESSIONS, enter, files, refused_in_one_line
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)

LAUNCHED = ["lane run (claude)", "architect started by the orchestrator", "docs started by the gate (codex)"]


@pytest.fixture(autouse=True)
def calm_machine(monkeypatch):
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")


def offline_project(tmp_path, monkeypatch):
    case = offline_local_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    for name in ("FORAGENT_ROLE", "FORAGENT_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    return case


def plan(case, capsys):
    """The lanes' public plan; a recorded stop shows up here."""
    capsys.readouterr()
    assert cli_exit(case.main, ["orch", "plan", "--json", "--repo-root", str(case.root)]) == 0
    return json.loads(capsys.readouterr().out)


def stop(case):
    return cli_exit(case.main, ["stop", "--why", "Accidental call from quoted text", "--repo-root", str(case.root)])


def stops_every_lane(case, capsys, monkeypatch, seat):
    """Guard: the person's terminal and the orchestrator keep `fa stop` exactly as today."""
    with monkeypatch.context() as scoped:
        for name in ("FORAGENT_ROLE", "FORAGENT_SESSION_ID"):
            scoped.delenv(name, raising=False)
        if seat == "orchestrator":  # The orchestrator's existing exempt marker.
            scoped.setenv("FORAGENT_ROLE", "orchestrator")
            scoped.setenv("FORAGENT_SESSION_ID", "orchestrator-session")
        assert cli_exit(case.main, ["orch", "release", "--repo-root", str(case.root)]) in (0, 1)
        lanes = plan(case, capsys)
        state = files(case.store.root)
        code = stop(case)
        output = capsys.readouterr()
        assert code == 0, f"The {seat} must keep fa stop: {output.err}"
        assert plan(case, capsys) != lanes, f"The {seat}'s stop must be recorded in the lanes' plan"
        assert files(case.store.root) != state


@pytest.mark.parametrize("session", LAUNCHED)
def test_fa_stop_is_person_only_inside_launched_sessions(
    tmp_path, monkeypatch, capsys, isolated_home, session,  # noqa: F811
):
    environment = SESSIONS[session](tmp_path, monkeypatch)  # Launch first: the offline project forbids executors.
    case = offline_project(tmp_path, monkeypatch)
    for seat in ("person", "orchestrator"):
        stops_every_lane(case, capsys, monkeypatch, seat)
    lanes = plan(case, capsys)
    state = files(case.store.root)
    with monkeypatch.context() as scoped:
        enter(scoped, environment)
        reference = refused_in_one_line(capsys, cli_exit(
            case.main, ["settings", "set", "review", "careful", "--repo-root", str(case.root)]))
        line = refused_in_one_line(capsys, stop(case))
    assert "settings set" in reference
    assert line == reference.replace("settings set", "stop"), (
        f"`fa stop` must give the existing person-only refusal, naming itself:\n{line}\n{reference}")
    assert files(case.store.root) == state, "A refused stop leaves no Foragent state behind"
    assert plan(case, capsys) == lanes, "A refused stop pauses no lane"
    assert case.gh_calls == []
