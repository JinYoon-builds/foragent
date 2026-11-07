"""Acceptance tests for promise-delivery: every owner's next session lists its due promises.

#2454 records promises (``fa orch promise add --when <event> --do <action> --owner <owner>``)
and lists a lane's due promises in that lane's next run prompt. This issue delivers due
promises to every owner, whichever path launches it:

- the orchestrator's next session (the gate's spawn, ``fa orch ensure``, ``fa orch refresh``
  and a whole ``fa gate tick`` that both sees the event and opens the orchestrator);
- a role session (the architect through ``fa orch architect --why``, docs through
  ``fa orch role docs --why``).

A due promise is one whose event already happened, whether or not a tick has marked it yet.
The prompt names its id and its action; a promise of another owner, or one still waiting,
is not listed.

Every check goes through public entry points: the real CLI (offline FakeTracker / gh record),
the gate's spawn functions and ``scheduler.tick`` with the launch command intercepted, and
the role's own session entrypoint (``fa gate run-architect``) run in-process. No real process
is started and no signal is sent (fake process tables only).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import re

import pytest

from test_skills_autosync import isolated_home  # noqa: F401 (fixture)


ORCH_ACTION = "publish order sky-7Q after the schema issue closes"
ARCHITECT_ACTION = "re-measure hot files of lane api-9K after the split"
DOCS_ACTION = "rewrite the mechanism map entry zebra-4M"
LANE_ACTION = "rebase the lane branch walrus-2J onto main"
PM_ACTION = "ask the person about pricing heron-5T"
WAITING_ACTION = "not yet: wait for the release otter-8V"


def mentions(text: str, token: str) -> int:
    return len(re.findall(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text))


def assert_listed(text: str, promise: dict, where: str) -> None:
    assert mentions(text, promise["id"]) >= 1, (
        f"{where} must list the due promise's id {promise['id']!r}:\n{text}")
    assert text.count(promise["do"]) == 1, (
        f"{where} must list the due promise's action exactly once ({promise['do']!r}):\n{text}")


def assert_not_listed(text: str, promise: dict, where: str, why: str) -> None:
    assert promise["do"] not in text, f"{where} must not list {why} ({promise['do']!r}):\n{text}"


# ---------------------------------------------------------------- orchestrator


def orch_promises(case, *, now):
    """Record promises through orch_promise.add (the function `fa orch promise add` runs).

    Time events need no tracker read. The orchestrator's promise became due a minute ago and
    no tick has marked it yet; the others belong to other owners or are still waiting.
    """
    from foragent import orch_promise

    past = (now - timedelta(minutes=1)).isoformat()
    future = (now + timedelta(days=2)).isoformat()
    owners = orch_promise.owners(case.config)

    def add(when, action, owner):
        return orch_promise.add(case.store, case.tracker, f"time:{when}", action, owner, now - timedelta(minutes=5),
                                known_owners=owners)

    return {
        "mine": add(past, ORCH_ACTION, "orchestrator"),
        "architect": add(past, ARCHITECT_ACTION, "architect"),
        "lane": add(past, LANE_ACTION, "main"),
        "pm": add(past, PM_ACTION, "pm"),
        "waiting": add(future, WAITING_ACTION, "orchestrator"),
    }


def launched_text(commands):
    claude = [argv for argv in commands if Path(argv[0]).name == "claude"]
    assert len(claude) == 1, f"The path must launch one orchestrator session: {commands}"
    return "\n".join(claude[0])


def launch_gate_spawn(case):
    from foragent.orch_spawn import ensure_orchestrator

    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background"


def launch_explicit_ensure(case):
    from foragent.orch_spawn import ensure_orchestrator

    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0], spacing=False) == "background"


def launch_refresh(case):
    from foragent.orch_spawn import spawn_orchestrator

    assert spawn_orchestrator(case.root, case.store, case.config, prompt="Continue from the handoff.",
                              now=case.clock[0]) is not None


@pytest.mark.parametrize("launch", [launch_gate_spawn, launch_explicit_ensure, launch_refresh],
                         ids=["gate-spawn", "orch-ensure", "orch-refresh"])
def test_orchestrators_next_session_lists_its_due_promise_id_and_action(git_history, tmp_path, monkeypatch, launch):
    from test_orch_spawn import orch_spawn_case

    case, commands, _ = orch_spawn_case(git_history, tmp_path, monkeypatch, screen="background")
    rows = orch_promises(case, now=case.clock[0])
    launch(case)
    text = launched_text(commands)

    assert_listed(text, rows["mine"], "The orchestrator's next session prompt")
    for key, why in (("architect", "the architect's promise"), ("lane", "a lane's promise"),
                     ("pm", "the PM's promise"), ("waiting", "a promise whose event has not happened")):
        assert_not_listed(text, rows[key], "The orchestrator's prompt", why)


def test_orchestrator_already_marked_due_by_a_tick_is_listed_at_its_next_launch(git_history, tmp_path, monkeypatch):
    """A promise a previous tick moved to due (and told the owner about) still reaches the next session."""
    from foragent import orch_promise
    from foragent.orch_spawn import ensure_orchestrator
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context
    from test_orch_spawn import orch_spawn_case

    case, commands, _ = orch_spawn_case(git_history, tmp_path, monkeypatch, screen="background")
    rows = orch_promises(case, now=case.clock[0])
    with monkeypatch.context() as quiet:
        quiet.setenv("FORAGENT_ORCH_SPAWN", "off")
        tick(tick_context(case, start_runs=False))
    states = {row["id"]: row["state"] for row in orch_promise.promises(case.store)}
    assert states[rows["mine"]["id"]] == "due", states
    commands.clear()

    case.clock[0] += timedelta(minutes=5)
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background"
    text = launched_text(commands)
    assert_listed(text, rows["mine"], "The orchestrator's next session prompt")
    assert_not_listed(text, rows["architect"], "The orchestrator's prompt", "the architect's promise")
    assert_not_listed(text, rows["waiting"], "The orchestrator's prompt", "a waiting promise")


def test_gate_tick_that_sees_the_event_and_opens_the_orchestrator_lists_the_promise(git_history, tmp_path, monkeypatch):
    """The #2454 review defect: the tick launched the orchestrator before it marked promises due."""
    from foragent import orch_promise
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context
    from test_orch_spawn import orch_spawn_case

    case, commands, _ = orch_spawn_case(git_history, tmp_path, monkeypatch, screen="background", seed_snapshot=False)
    rows = orch_promises(case, now=case.clock[0])
    tick(tick_context(case, start_runs=False))
    text = launched_text(commands)

    assert_listed(text, rows["mine"], "The orchestrator session this tick opened")
    for key in ("architect", "lane", "pm", "waiting"):
        assert_not_listed(text, rows[key], "The orchestrator's prompt", f"the {key} promise")
    states = {row["id"]: row["state"] for row in orch_promise.promises(case.store)}
    assert states[rows["mine"]["id"]] == "due", f"The same tick still marks the promise due: {states}"


# ---------------------------------------------------------------- roles


def cli_promise(case, capsys, when, action, owner):
    import json

    from test_orch_inbox import inbox_command

    capsys.readouterr()
    code = inbox_command(case, "promise", "add", "--when", when, "--do", action, "--owner", owner)
    output = capsys.readouterr()
    assert code == 0, f"fa orch promise add must record the promise: {output.err}{output.out}"
    assert inbox_command(case, "promise", "list", "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    (row,) = [row for row in listed if row.get("do") == action]
    return row


def role_promises(case, capsys):
    now = datetime.now(UTC)
    past = (now - timedelta(minutes=30)).isoformat()
    future = (now + timedelta(days=2)).isoformat()
    return {
        "architect": cli_promise(case, capsys, f"time:{past}", ARCHITECT_ACTION, "architect"),
        "docs": cli_promise(case, capsys, f"time:{past}", DOCS_ACTION, "docs"),
        "orchestrator": cli_promise(case, capsys, f"time:{past}", ORCH_ACTION, "orchestrator"),
        "lane": cli_promise(case, capsys, f"time:{past}", LANE_ACTION, "main"),
        "pm": cli_promise(case, capsys, f"time:{past}", PM_ACTION, "pm"),
        "waiting-architect": cli_promise(case, capsys, f"time:{future}", WAITING_ACTION, "architect"),
    }


def role_session_prompt(case, capsys, role):
    from test_orch_architect import WHY, finish_session
    from test_orch_inbox import inbox_command

    capsys.readouterr()
    args = ("architect", "--why", WHY) if role == "architect" else ("role", role, "--why", WHY)
    code = inbox_command(case, *args)
    output = capsys.readouterr()
    assert code == 0, f"fa orch {' '.join(args[:2])} must start the role session: {output.err}"
    assert len(case.launches) == 1, case.launches
    finish_session(case, case.launches[0])
    return case.turns[0]["prompt"]


@pytest.mark.parametrize("role", ["architect", "docs"])
def test_role_session_lists_its_own_due_promise_and_no_other_owners(tmp_path, monkeypatch, capsys, role):
    from test_orch_architect import architect_case

    case = architect_case(tmp_path, monkeypatch)
    rows = role_promises(case, capsys)
    prompt = role_session_prompt(case, capsys, role)

    assert_listed(prompt, rows[role], f"The {role} session prompt")
    others = [key for key in ("architect", "docs", "orchestrator", "lane", "pm") if key != role]
    for key in others:
        assert_not_listed(prompt, rows[key], f"The {role} prompt", f"the {key} owner's promise")
        assert mentions(prompt, rows[key]["id"]) == 0, (
            f"The {role} prompt must not name another owner's promise id {rows[key]['id']!r}:\n{prompt}")
    assert_not_listed(prompt, rows["waiting-architect"], f"The {role} prompt", "a promise still waiting")
