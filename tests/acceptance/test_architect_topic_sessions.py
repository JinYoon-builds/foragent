"""Acceptance test for architect-topic-sessions (#1470). Locked by Foragent; do not edit.

An architect session lives for one topic: when the orchestrator rebuts its proposal, the same
executor session resumes with the rebuttal, for at most three architect turns in all, and then both
sides go to the person's PM as one question. Agreement closes the topic; the next topic starts fresh.
"""

import json
import re

from foragent import executors, runner
from foragent.executors import ExecResult
from test_orch_architect import architect_case
from test_orch_inbox import inbox_command, inbox_rows
from test_skills_autosync import cli_exit


TOPIC = "test_runner.py takes 65 % of unit-test time; six runs waited about an hour for test slots"
OTHER_TOPIC = "Three lanes edit scheduler.py in the same week; 11 overlap waits in 7 days"
REBUTTAL = "Rebuttal {n}: the 65 % share comes from one fixture, not from the lane boundary"


def topic_case(tmp_path, monkeypatch):
    """The architect harness, with an agent that proposes once per turn and reports its session id.

    A fresh executor session gets a new id; a resumed one keeps the id it resumed.
    """
    case = architect_case(tmp_path, monkeypatch)
    case.sessions = 0

    def execute(**kwargs):
        case.turns.append(kwargs)
        resumed = kwargs.get("resume_session_id")
        if resumed is None:
            case.sessions += 1
        session = resumed or f"architect-topic-session-{case.sessions}"
        text = (f"Decision: orchestrator\nProposal turn {len(case.turns)}: move the heavy fixtures of "
                "test_runner.py to a shared testing lane. Expected effect: share from 65 % to 40 %.")
        assert inbox_command(case, "request", "--from", "architect", "--text", text) == 0
        return ExecResult(0, False, f"Proposal turn {len(case.turns)} recorded.", kwargs["log_path"],
                          session_id=session)

    fake = type("FakeExecutor", (), {"run": staticmethod(execute)})()
    monkeypatch.setattr(executors, "executor_for", lambda *a, **kw: fake)
    monkeypatch.setattr(runner, "executor_for", lambda *a, **kw: fake)
    for executor in (executors.ClaudeExecutor, executors.CodexExecutor):
        monkeypatch.setattr(executor, "run", lambda self, **kwargs: execute(**kwargs))
    return case


def drive(case):
    """Run every role session launched so far, the way the launched child would run it.

    The orchestrator's own command (the call or the decision) is what starts a turn; no tick is needed.
    """
    for child in case.launches:
        if child.returncode is None:
            child.returncode = cli_exit(case.main, child.args[3:])
            assert child.returncode == 0, "The role session entrypoint must run the fake agent"


def start_topic(case, capsys, why):
    capsys.readouterr()
    code = inbox_command(case, "architect", "--why", why)
    output = capsys.readouterr()
    assert code == 0, f"fa orch architect --why must start a session: {output.err.strip()}"
    drive(case)


def latest_proposal(case, capsys):
    open_rows = [row for row in inbox_rows(case, capsys)
                 if row["from"] == "architect" and row["status"] != "done"]
    assert open_rows, "The architect turn must leave its proposal in the orchestrator inbox"
    return open_rows[-1]


def decide(case, capsys, row, decision, answer):
    capsys.readouterr()
    code = inbox_command(case, "role-decision", row["id"], decision, "--answer", answer)
    output = capsys.readouterr()
    assert code == 0, f"fa orch role-decision must record the decision: {output.err.strip()}"
    drive(case)


def person_questions(case, capsys):
    return inbox_rows(case, capsys, "questions")


def test_a_rebuttal_resumes_the_same_architect_session_with_the_rebuttal(tmp_path, monkeypatch, capsys):
    case = topic_case(tmp_path, monkeypatch)
    start_topic(case, capsys, TOPIC)
    assert len(case.turns) == 1
    first = case.turns[0]
    assert first.get("resume_session_id") is None, "A new topic starts a fresh session"

    decide(case, capsys, latest_proposal(case, capsys), "rejected", REBUTTAL.format(n=1))
    assert len(case.turns) == 2, "One rebuttal resumes the architect exactly once"
    second = case.turns[1]
    assert second.get("resume_session_id") == "architect-topic-session-1", (
        "The rebuttal must resume the topic's own executor session, not start a new one")
    assert REBUTTAL.format(n=1) in second["prompt"], "The resumed turn must receive the orchestrator's rebuttal"
    assert TOPIC in first["prompt"]
    assert person_questions(case, capsys) == [], "One rebuttal is not yet a disagreement for the PM"
    assert case.gh_calls == [] and case.pushes == []


def test_agreement_closes_the_topic_and_the_next_topic_starts_fresh(tmp_path, monkeypatch, capsys):
    case = topic_case(tmp_path, monkeypatch)
    start_topic(case, capsys, TOPIC)
    decide(case, capsys, latest_proposal(case, capsys), "rejected", REBUTTAL.format(n=1))
    assert len(case.turns) == 2 and case.turns[1].get("resume_session_id") == "architect-topic-session-1"

    decide(case, capsys, latest_proposal(case, capsys), "accepted", "Ordered as the shared testing lane")
    assert len(case.turns) == 2, "Agreement closes the topic: the architect does not resume"
    assert person_questions(case, capsys) == [], "An agreed topic does not go to the PM"

    start_topic(case, capsys, OTHER_TOPIC)
    assert len(case.turns) == 3
    third = case.turns[2]
    assert third.get("resume_session_id") is None, "The next topic starts a fresh session"
    assert OTHER_TOPIC in third["prompt"] and REBUTTAL.format(n=1) not in third["prompt"]
    assert case.gh_calls == [] and case.pushes == []


def test_a_topic_still_disputed_after_the_round_limit_goes_to_the_pm_with_both_sides(
    tmp_path, monkeypatch, capsys,
):
    case = topic_case(tmp_path, monkeypatch)
    start_topic(case, capsys, TOPIC)
    rebuttals = []
    for n in range(1, 5):
        turns = len(case.turns)
        proposal = latest_proposal(case, capsys)
        rebuttals.append((proposal, REBUTTAL.format(n=n)))
        decide(case, capsys, proposal, "rejected", REBUTTAL.format(n=n))
        if len(case.turns) == turns:
            break
    assert 2 <= len(case.turns) <= 3, (
        f"A disputed topic resumes for two or three architect turns in all, not {len(case.turns)}")
    assert all(turn.get("resume_session_id") == "architect-topic-session-1" for turn in case.turns[1:])

    last_proposal, last_rebuttal = rebuttals[-1]
    asked = [row for row in person_questions(case, capsys)
             if last_rebuttal in json.dumps(row, ensure_ascii=False)]
    assert len(asked) == 1, "Exactly one open PM question carries the disputed topic"
    question = json.dumps(asked[0], ensure_ascii=False)
    marker = re.search(r"Proposal turn \d+", last_proposal["text"])[0]
    assert marker in question, "The PM question carries the architect's last proposal as well"

    turns = len(case.turns)
    drive(case)
    assert len(case.turns) == turns, "After the PM handoff the architect no longer resumes on its own"
    assert len([row for row in person_questions(case, capsys)
                if last_rebuttal in json.dumps(row, ensure_ascii=False)]) == 1, "The handoff is not repeated"
    assert case.gh_calls == [] and case.pushes == []
