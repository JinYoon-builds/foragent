"""Acceptance contract: a planning session starts knowing which fa command does each PM task.

The PM once opened an Orca tab for the dashboard because it did not know
`fa gate watch --open` exists. The SessionStart hook gives the planning session a
short "PM task -> fa command" table as additionalContext. The table comes from the
CLI's own parser, so a renamed or removed command changes it; lane sessions never
get it. The person-facing systemMessage is untouched (test_presence_session_start).
"""

import argparse
import re

import pytest

from test_fa_report_since import report_case  # noqa: F401 -- session_case dependency
from test_presence_session_start import session_case  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401

# PM tasks the table must cover; each is a fa command in backticks, arguments allowed.
REQUIRED = {
    "open the dashboard": r"fa gate watch --open",
    "ask the orchestrator": r"fa orch request",
    "retry an issue": r"fa orch retry",
    "stop an issue": r"fa orch stop",
    "read open questions": r"fa orch questions",
    "see the state": r"fa (?:brief|status)",
}
WORD = re.compile(r"^[a-z][a-z0-9-]*$")


def context_of(payload):
    output = payload.get("hookSpecificOutput") or {}
    if not output:
        return ""
    assert output.get("hookEventName") == "SessionStart", output
    return output.get("additionalContext") or ""


def quoted_commands(context):
    return re.findall(r"`(fa(?: [^`\n]+)?)`", context)


def occurrences(context, command):
    return len(re.findall(r"`" + command + r"(?:\s[^`\n]*)?`", context))


@pytest.fixture
def planning(session_case, monkeypatch):
    monkeypatch.delenv("FORAGENT_ROLE", raising=False)
    return session_case


def test_planning_session_start_carries_the_command_table_once(planning):
    payload = planning.hook()
    context = context_of(payload)
    for task, command in REQUIRED.items():
        assert occurrences(context, command) == 1, (
            f"The PM table must name the command for '{task}' ({command}) exactly once:\n{context}")
    # A resumed or second planning session gets its own single table, not an accumulated one.
    again = context_of(planning.hook("offline-session-B"))
    assert occurrences(again, REQUIRED["open the dashboard"]) == 1, again


def test_every_command_in_the_table_exists_in_the_cli_help(planning, capsys):
    from foragent import cli

    commands = quoted_commands(context_of(planning.hook()))
    assert len(commands) >= len(REQUIRED), f"Expected the PM command table; got {commands}"
    for command in commands:
        tokens = command.split()[1:]
        words = []
        for token in tokens:
            if not WORD.match(token):
                break
            words.append(token)
        flags = [token for token in tokens if re.match(r"^--[a-z][a-z0-9-]*$", token)]
        capsys.readouterr()
        assert cli_exit(cli.main, [*words, "--help"]) == 0, f"`{command}` is not a fa command"
        shown = " ".join(capsys.readouterr().out.split())
        assert f"usage: fa {' '.join(words)}" in shown, f"`{command}` must name a real subcommand: {shown[:200]}"
        for flag in flags:
            assert flag in shown, f"`{command}` uses {flag}, which `fa {' '.join(words)} --help` lacks"


def test_renamed_or_removed_command_changes_the_table(planning, monkeypatch):
    original = argparse._SubParsersAction.add_parser

    def add_parser(self, name, **kwargs):
        if name == "questions":
            return argparse.ArgumentParser(add_help=False)  # built but never registered
        return original(self, "redo" if name == "retry" else name, **kwargs)

    monkeypatch.setattr(argparse._SubParsersAction, "add_parser", add_parser)
    context = context_of(planning.hook())
    assert occurrences(context, r"fa orch redo") == 1, f"A renamed command must appear under its new name:\n{context}"
    assert occurrences(context, r"fa orch retry") == 0, "The table must not keep a hard-coded old name"
    assert occurrences(context, r"fa orch questions") == 0, "A removed command must leave the table"
    assert occurrences(context, REQUIRED["open the dashboard"]) == 1, "Unchanged commands stay"


@pytest.mark.parametrize("role", ["view", ""], ids=["lane", "empty-role"])
def test_lane_sessions_never_get_the_command_table(planning, monkeypatch, role):
    assert occurrences(context_of(planning.hook()), REQUIRED["open the dashboard"]) == 1, "Control: planning"
    monkeypatch.setenv("FORAGENT_ROLE", role)
    payload = planning.hook("offline-session-lane")
    assert "fa gate watch --open" not in context_of(payload) and "fa gate watch --open" not in str(payload), (
        "A session with FORAGENT_ROLE set is a lane or role session, not the PM")
