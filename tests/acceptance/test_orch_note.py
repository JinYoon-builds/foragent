"""Acceptance test for orch-note. Locked by Foragent; do not edit."""

from datetime import UTC, datetime
from pathlib import Path
import re

import pytest

from test_order_progress import offline_local_cli
from test_skills_autosync import cli_exit, isolated_home


@pytest.fixture(autouse=True)
def clean_note_location(monkeypatch):
    for key in ("ORCA_TERMINAL_HANDLE", "FORAGENT_ORCH_TITLE", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(key, raising=False)


def note_command(case, *args):
    return cli_exit(case.main, ["orch", "note", *args, "--repo-root", str(case.root)])


def read_note(case):
    from foragent.state import StateStore

    reader = getattr(StateStore(case.store.root), "orch_note", None)
    assert callable(reader), "StateStore.orch_note() must expose the latest persisted note"
    return reader()


def write_note(case, *extra, now="원인을 보고 있어요", why="시험과 문서가 달라요", next="시험을 고칠게요"):
    before = datetime.now(UTC)
    assert note_command(case, "--now", now, "--why", why, "--next", next, *extra) == 0
    note = read_note(case)
    assert {"now", "why", "next", "at", "where"} <= note.keys()
    assert (note["now"], note["why"], note["next"]) == (now, why, next)
    assert before <= datetime.fromisoformat(note["at"]) <= datetime.now(UTC)
    assert case.gh_calls == []
    return note


def test_orch_note_orca_location_title_override_and_latest_note_survive_reopen(tmp_path, monkeypatch):
    case = offline_local_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "offline-terminal-7")
    monkeypatch.setenv("FORAGENT_ORCH_TITLE", "orch: Env project")
    # Orca wins even if it is running inside tmux.
    monkeypatch.setenv("TMUX", "/offline/tmux,1,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    note = write_note(case, "--where-title", "orch: Foragent")
    assert note["where"]["kind"] == "orca"
    assert note["where"]["handle"] == "offline-terminal-7"
    assert note["where"]["title"] == "orch: Foragent"
    latest = write_note(case, now="다시 맡겼어요", next="결과를 기다릴게요")
    assert latest["where"]["title"] == "orch: Env project"
    assert latest["now"] != note["now"] and read_note(case) == latest


def test_orch_note_tmux_and_plain_terminal_are_facts_not_terminal_probes(tmp_path, monkeypatch):
    case = offline_local_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("TMUX", "/offline/tmux,12,0")
    monkeypatch.setenv("TMUX_PANE", "%12")
    note = write_note(case)
    assert note["where"]["kind"] == "tmux" and note["where"]["pane"] == "%12"
    monkeypatch.delenv("TMUX")
    monkeypatch.delenv("TMUX_PANE")
    assert write_note(case)["where"]["kind"] == "terminal"


def test_orch_note_missing_now_rejects_without_overwriting_latest_note(tmp_path, monkeypatch):
    case = offline_local_cli(tmp_path, monkeypatch)
    assert read_note(case) is None
    before = write_note(case)
    assert note_command(case, "--why", "A reason", "--next", "A next action") != 0
    assert read_note(case) == before and case.gh_calls == []


def test_orch_note_skill_updates_each_action_in_config_language_and_names_session():
    text = " ".join((Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md").read_text().lower().split())
    assert "fa orch note" in text
    for flag in ("--now", "--why", "--next"):
        assert flag in text
    assert re.search(r"(?:each|every).{0,60}action|action.{0,60}(?:each|every)", text)
    assert "language" in text and "docs/voice.md" in text
    assert "orch: <project>" in text
