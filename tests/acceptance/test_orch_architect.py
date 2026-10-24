"""Acceptance test for orch-architect. Locked by Foragent; do not edit."""

from datetime import datetime
import json
import os
import re
import subprocess
from types import SimpleNamespace

import pytest

from test_local_truth import record_gh
from test_order_progress import offline_local_cli
from test_orch_inbox import inbox_command, inbox_rows
from test_skills_autosync import cli_exit, isolated_home


WHY = "test_runner.py takes 65 % of unit-test time; six runs waited about an hour for test slots"
PROPOSAL = "Proposal: share the heavy test_runner.py fixtures, then measure the unit-test time again."


def architect_events(store):
    path = store.root / "orch-events.jsonl"
    return [row for line in (path.read_text().splitlines() if path.exists() else [])
            if (row := json.loads(line))["kind"] == "architect-call"]


def architect_case(tmp_path, monkeypatch):
    """Intercept the worker launcher boundary and run its child only on demand.

    No Popen, executor turn, notification or process signal reaches the machine.
    The fake agent uses the existing ask tool, rather than requiring a script to
    interpret model prose as a decision or to publish an order.
    """
    from foragent import executors, notify, runner
    from foragent.executors import ExecResult
    from foragent.launcher import spawn_runner
    from foragent.orch_inbox import write_plan_heartbeat

    case = offline_local_cli(tmp_path, monkeypatch)
    (case.root / ".foragent.yml").write_text(
        "repo: owner/repo\nlanguage: en\nskill_libraries: []\nnotify: none\n")
    case.launches, case.turns, case.pushes = [], [], []
    case.propose = False

    class Session:
        def __init__(self, argv, **kwargs):
            assert not isinstance(argv, str), "Use the worker launcher's argv boundary"
            self.args = [str(part) for part in argv]
            self.kwargs = kwargs
            self.pid = 980000 + len(case.launches)
            self.returncode = None
            case.launches.append(self)

        def poll(self):
            return self.returncode

        def wait(self, *args, **kwargs):
            assert self.returncode is not None, "Do not wait for the architect in the caller"
            return self.returncode

        def terminate(self):
            pytest.fail("The architect command must not terminate another session")

        def kill(self):
            pytest.fail("The architect command must not kill another session")

    def probe(pid, sig):
        assert sig == 0, "Only fake liveness probes are allowed; never signal a real process"
        if not any(session.pid == pid and session.poll() is None for session in case.launches):
            raise ProcessLookupError(pid)

    def no_group_signal(*args, **kwargs):
        pytest.fail("Never signal real process groups in architect acceptance tests")

    def execute(**kwargs):
        case.turns.append(kwargs)
        if case.propose:
            assert inbox_command(case, "ask", "--question", PROPOSAL,
                                 "--option", "Approve a tidy order (recommended)",
                                 "--option", "Defer", "--recommend", "1") == 0
        return ExecResult(0, False, "Proposal recorded; waiting for the person's decision.",
                          kwargs["log_path"], session_id="offline-architect-session")

    fake_executor = SimpleNamespace(run=execute)
    monkeypatch.setattr(subprocess, "Popen", Session)
    monkeypatch.setattr(os, "kill", probe)
    monkeypatch.setattr(os, "killpg", no_group_signal)
    monkeypatch.setattr(executors, "executor_for", lambda *a, **kw: fake_executor)
    monkeypatch.setattr(runner, "executor_for", lambda *a, **kw: fake_executor)
    for executor in (executors.ClaudeExecutor, executors.CodexExecutor):
        monkeypatch.setattr(executor, "run", lambda self, **kwargs: execute(**kwargs))
    monkeypatch.setattr(notify, "send", lambda *a, **kw: case.pushes.append((a, kw)))
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    case.gh_calls = record_gh(monkeypatch)
    write_plan_heartbeat(case.store)  # Proposals go to the live planning session's box.

    # Obtain the real worker entrypoint prefix without starting any worker.
    spawn_runner(case.root, case.store)(999)
    worker = case.launches.pop()
    assert worker.args[1:3] == ["-m", "foragent.cli"]
    case.worker_prefix = worker.args[:3]
    return case


def call_architect(case, capsys, why=WHY):
    capsys.readouterr()
    code = inbox_command(case, "architect", "--why", why)
    output = capsys.readouterr()
    assert code == 0, f"fa orch architect --why must start a session: {output.err.strip()}"
    assert len(case.launches) == 1, "Launch one independent architect through the worker launcher path"
    child, = case.launches
    assert child.args[:3] == case.worker_prefix, "Use the same foragent session entrypoint as gate workers"
    assert child.kwargs.get("start_new_session") is True, "The architect is a separate session"
    assert child.kwargs.get("stdout") is not None and child.kwargs.get("stderr") is not None, (
        "Keep the worker launcher's durable log streams")
    assert case.turns == [], "The caller must return while the architect session is running"
    return child


def finish_session(case, child):
    # Run the recorded child command synchronously under fake executor methods.
    # No internal verb name is prescribed; only the existing worker entrypoint.
    code = cli_exit(case.main, child.args[3:])
    child.returncode = code
    assert code == 0, "The architect session entrypoint must execute the fake agent successfully"
    assert len(case.turns) == 1, "Run the architect through the existing executor interface"


def test_orch_architect_launches_with_evidence_measurements_refactor_and_a_local_call_event(
    tmp_path, monkeypatch, capsys,
):
    case = architect_case(tmp_path, monkeypatch)
    before = datetime.now().astimezone()
    child = call_architect(case, capsys)
    event, = architect_events(case.store)
    assert WHY in event.get("why", event.get("summary", "")), "Journal the caller's evidence verbatim"
    at = datetime.fromisoformat(event["at"])
    assert at.utcoffset() is not None and before <= at <= datetime.now().astimezone()
    finish_session(case, child)
    prompt = case.turns[0]["prompt"]
    assert WHY in prompt
    for command in ("fa orch facts", "skills/fa-orchestrator/references/refactor.md"):
        assert command in prompt, f"Give the architect the existing measurement/procedure tool: {command}"
    assert re.search(r"hot[ -]files", prompt, re.I), "Measure file contention before proposing a split"
    assert re.search(r"pytest\b.{0,160}--durations", prompt, re.I | re.S), "Measure slow tests as well as hot files"
    assert "fa orch ask" in prompt and re.search(r"propos", prompt, re.I), "Send proposals to the person's decision list"
    assert re.search(r"(?:human|person|user|jin).{0,140}approv|approv.{0,140}(?:human|person|user|jin)",
                     prompt, re.I | re.S), "The architect proposes tidy orders for human approval"
    assert len(architect_events(case.store)) == 1, "Executing the child must not count as a second call"
    assert case.gh_calls == [] and case.pushes == []


def test_orch_architect_proposal_lands_in_existing_questions_without_publishing_orders(
    tmp_path, monkeypatch, capsys,
):
    from foragent.state import StateStore

    case = architect_case(tmp_path, monkeypatch)
    case.propose = True
    assert inbox_rows(case, capsys, "questions") == []
    child = call_architect(case, capsys)
    finish_session(case, child)
    question, = inbox_rows(case, capsys, "questions")
    assert question["question"] == PROPOSAL and question["status"] == "open" and question["answer"] is None
    assert question["options"] == ["Approve a tidy order (recommended)", "Defer"] and question["recommend"] == 1
    # Read through the existing API on a fresh store, not a new private proposal queue.
    from foragent.orch_inbox import questions

    assert questions(StateStore(case.store.root)) == [question]
    assert not list((case.store.root / "approvals").glob("*.json")), "A proposal is not an approved order"
    assert not list((case.store.root / "runs").glob("*.json")), "Do not turn a proposal into lane work"
    assert case.store.local_issues() == [] and case.gh_calls == [] and case.pushes == []
    assert len(case.launches) == 1


@pytest.mark.parametrize("arguments", [(), ("--why", ""), ("--why", "   ")],
                         ids=["missing", "empty", "whitespace"])
def test_orch_architect_requires_nonempty_why_without_launch_or_call_event(
    tmp_path, monkeypatch, capsys, arguments,
):
    case = architect_case(tmp_path, monkeypatch)
    capsys.readouterr()
    assert inbox_command(case, "architect", *arguments) != 0
    output = capsys.readouterr()
    assert re.search(r"--why|evidence|근거", output.out + output.err, re.I), "Explain the required evidence argument"
    assert case.launches == [] and architect_events(case.store) == [] and case.turns == []
    # An absent command must not satisfy the negative argument test.
    call_architect(case, capsys)


def test_orch_architect_refuses_a_second_live_session_but_allows_a_later_call(
    tmp_path, monkeypatch, capsys,
):
    case = architect_case(tmp_path, monkeypatch)
    child = call_architect(case, capsys)
    # Each offline CLI call reopens StateStore: the guard must survive sessions.
    assert inbox_command(case, "architect", "--why", "Another hot file needs inspection") != 0
    output = capsys.readouterr()
    assert re.search(r"architect|아키텍트", output.out + output.err, re.I)
    assert re.search(r"already|alive|running|active|이미|실행 중", output.out + output.err, re.I), (
        "Explain that an architect session is still alive")
    assert len(case.launches) == len(architect_events(case.store)) == 1
    assert case.turns == []
    finish_session(case, child)
    assert inbox_command(case, "architect", "--why", "Measure again after the first proposal") == 0
    assert len(case.launches) == len(architect_events(case.store)) == 2, "The session guard is not a permanent lock"
    assert case.gh_calls == []
