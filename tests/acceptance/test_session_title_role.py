"""Acceptance: the SessionStart hook names the session's Foragent role in the title.

Claude Code (2.1.317, verified in the installed package's hook output schema)
reads a SessionStart hook's ``hookSpecificOutput.sessionTitle`` (with
``hookEventName: "SessionStart"``) as the session title, and terminals show it
in their title bar. Foragent's installed hook (`fa statusline --session-start`)
returns a title naming the role (PM, orchestrator, architect, docs, lane) and
the project, so a person can tell the windows apart. A session outside a
Foragent project gets no title. Observed only through the installed hook entry
with Claude Code's SessionStart stdin; exact wording is the lane's choice.
"""

from io import BytesIO, TextIOWrapper
import json
import re
import sys

import pytest

from foragent import cli
from test_fa_report_since import report_case  # noqa: F401
from test_presence_session_start import session_case  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


PROJECT = "reports"  # The project's name: the short name of the configured repo offline/reports.
ROLE_WORDS = {
    "pm": r"\bPM\b",
    "orchestrator": r"orchestrator",
    "architect": r"architect",
    "docs": r"\bdocs?\b",
    "lane": r"\blane\b",
}
MASCOT = ("squirrel", "acorn", "다람쥐", "도토리", "🐿", "🌰")

# How each role's session starts: the environment Foragent's launchers give it and
# the `--agent` name Claude Code reports as the hook's agent_type.
SESSIONS = {
    "pm": ({}, None),
    "orchestrator": ({"FORAGENT_ROLE": "orchestrator", "FORAGENT_SESSION_ID": "orch-1"}, "fa-orchestrator"),
    "architect": ({"FORAGENT_ROLE": "launched"}, "fa-architect"),
    "docs": ({"FORAGENT_ROLE": "launched"}, "fa-docs"),
    "lane": ({"FORAGENT_ROLE": "launched"}, "fa-api"),
}


def _start(case, monkeypatch, *, env, agent_type, cwd, session_id="offline-title-session"):
    """Run the installed SessionStart command with Claude Code's stdin; return its JSON (or {})."""
    argv = case.hook_argv()
    payload = {"hook_event_name": "SessionStart", "source": "startup", "session_id": session_id,
               "cwd": str(cwd), "transcript_path": str(cwd / f"{session_id}.jsonl")}
    if agent_type is not None:
        payload["agent_type"] = agent_type
    stdin = TextIOWrapper(BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8")
    stdout, stderr = [TextIOWrapper(BytesIO(), encoding="utf-8") for _ in range(2)]
    with monkeypatch.context() as scoped:
        for name in ("FORAGENT_ROLE", "FORAGENT_SESSION_ID"):
            scoped.delenv(name, raising=False)
        for name, value in env.items():
            scoped.setenv(name, value)
        scoped.setattr(sys, "stdin", stdin)
        scoped.setattr(sys, "stdout", stdout)
        scoped.setattr(sys, "stderr", stderr)
        code = cli_exit(cli.main, argv)
        stdout.flush()
        stderr.flush()
        output, error = stdout.buffer.getvalue().decode("utf-8"), stderr.buffer.getvalue()
    assert code == 0, f"The SessionStart hook must succeed: {error!r}"
    assert not error, "The SessionStart hook must print nothing on stderr"
    if not output.strip():
        return {}
    try:
        result = json.loads(output)
    except ValueError:
        pytest.fail(f"SessionStart stdout must be exactly one JSON object, got {output!r}")
    assert isinstance(result, dict)
    return result


def _title(result):
    """The title from the field Claude Code reads; nowhere else."""
    assert "sessionTitle" not in result, "Claude Code reads sessionTitle only under hookSpecificOutput"
    specific = result.get("hookSpecificOutput")
    assert isinstance(specific, dict), "The title goes in hookSpecificOutput"
    assert specific.get("hookEventName") == "SessionStart", "hookSpecificOutput must name its SessionStart event"
    title = specific.get("sessionTitle")
    assert isinstance(title, str) and title.strip(), "hookSpecificOutput.sessionTitle must be a non-empty string"
    assert len(title.splitlines()) == 1, "A terminal title is one line"
    return title


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("role", list(SESSIONS))
def test_session_title_names_the_role_and_the_project(session_case, monkeypatch, role, language):
    case = session_case
    case.configure(language=language)
    env, agent_type = SESSIONS[role]
    result = _start(case, monkeypatch, env=env, agent_type=agent_type, cwd=case.root)
    title = _title(result)
    assert re.search(ROLE_WORDS[role], title, re.I), f"The {role} session's title must name its role: {title!r}"
    for other, pattern in ROLE_WORDS.items():
        if other != role:
            assert not re.search(pattern, title, re.I), f"The {role} title must not name the {other} role: {title!r}"
    assert PROJECT in title, f"The title must name the project: {title!r}"
    if role == "lane":
        assert re.search(r"\bapi\b", title), f"A lane's title must name which lane: {title!r}"
    assert not any(word in title.lower() for word in MASCOT), "Role names use industry terms, not mascot words"
    assert isinstance(result.get("systemMessage"), str) and result["systemMessage"].strip(), (
        "The title is added beside the existing one-line systemMessage, not instead of it")


def test_each_lane_window_gets_its_own_lane_name(session_case, monkeypatch):
    case = session_case
    titles = {agent: _title(_start(case, monkeypatch, env={"FORAGENT_ROLE": "launched"},
                                   agent_type=agent, cwd=case.root))
              for agent in ("fa-api", "fa-decision")}
    assert re.search(r"\bapi\b", titles["fa-api"]) and "decision" not in titles["fa-api"]
    assert re.search(r"\bdecision\b", titles["fa-decision"]) and not re.search(r"\bapi\b", titles["fa-decision"])


def test_session_outside_a_foragent_project_gets_no_title(session_case, monkeypatch, tmp_path):
    case = session_case
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _title(_start(case, monkeypatch, env={}, agent_type=None, cwd=case.root))  # Same hook, project: titled.
    result = _start(case, monkeypatch, env={}, agent_type=None, cwd=elsewhere)
    assert "sessionTitle" not in json.dumps(result), (
        "A person's own session outside a Foragent project keeps its title: no sessionTitle anywhere")
