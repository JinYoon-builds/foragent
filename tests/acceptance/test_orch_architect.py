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
PROPOSAL = (
    "Decision: person\n"
    "Proposal: change lane boundaries so a shared testing lane owns the heavy test_runner.py fixtures. "
    f"Evidence (2026-10-24 incident): {WHY}. "
    "Expected measurable effect: reduce test_runner.py's share of unit-test time from 65 % to 40 %; "
    "measure again with pytest --durations."
)


def architect_events(store):
    path = store.root / "orch-events.jsonl"
    return [row for line in (path.read_text().splitlines() if path.exists() else [])
            if (row := json.loads(line))["kind"] == "architect-call"]


def architect_case(tmp_path, monkeypatch):
    """Intercept the worker launcher boundary and run its child only on demand.

    No Popen, executor turn, notification or process signal reaches the machine.
    The fake agent delivers a proposal through the existing request tool for the
    orchestrator to judge, without a script interpreting prose or publishing orders.
    """
    from foragent import executors, notify, processes, runner
    from foragent.executors import ExecResult
    from foragent.launcher import spawn_runner
    from foragent.state import StateStore

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
            assert inbox_command(case, "request", "--from", "architect", "--text", PROPOSAL) == 0
        return ExecResult(0, False, "Proposal recorded for the orchestrator's judgment."
                          if case.propose else "Nothing to change.",
                          kwargs["log_path"], session_id="offline-architect-session")

    fake_executor = SimpleNamespace(run=execute)
    monkeypatch.setattr(subprocess, "Popen", Session)
    # The table and probes only contain fake sessions, never host PIDs.
    monkeypatch.setattr(processes, "list_processes", lambda: processes.ProcessTable([
        processes.ProcessIdentity(s.pid, 1, s.pid, s.pid, float(s.pid))
        for s in case.launches if s.poll() is None]))
    monkeypatch.setattr(os, "kill", probe)
    monkeypatch.setattr(os, "killpg", no_group_signal)
    monkeypatch.setattr(executors, "executor_for", lambda *a, **kw: fake_executor)
    monkeypatch.setattr(runner, "executor_for", lambda *a, **kw: fake_executor)
    for executor in (executors.ClaudeExecutor, executors.CodexExecutor):
        monkeypatch.setattr(executor, "run", lambda self, **kwargs: execute(**kwargs))
    monkeypatch.setattr(notify, "send", lambda *a, **kw: case.pushes.append((a, kw)))
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    case.gh_calls = record_gh(monkeypatch)
    # The real launcher now records process identity on an existing run. Keep
    # this probe in its own store so the architect still starts with no lane work.
    probe_store = StateStore(tmp_path / "worker-probe")
    probe_store.start_run(999, branch="foragent/issue-999", worktree=probe_store.worktree_path(999))
    spawn_runner(case.root, probe_store)(999)
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
    assert re.search(r"propos.{0,180}fa orch request|fa orch request.{0,180}propos",
                     prompt, re.I | re.S), "Deliver proposals to the orchestrator's durable inbox"
    assert re.search(r"orchestrat.{0,140}(?:decid|judg|choos).{0,140}order"
                     r"|(?:decid|judg|choos).{0,140}order.{0,140}orchestrat",
                     prompt, re.I | re.S), "The orchestrator decides routine proposals and handles person decisions below"
    assert re.search(r"(?:only|just).{0,40}propos|propos.{0,40}(?:only|just)",
                     prompt, re.I | re.S), "The architect's role is to propose"
    assert re.search(r"(?:nothing to change|no changes? (?:are )?needed).{0,120}(?:valid|acceptable|good|best)"
                     r"|(?:valid|acceptable|good|best).{0,120}(?:nothing to change|no changes? (?:are )?needed)",
                     prompt, re.I | re.S), "Explain that nothing to change is a valid outcome"
    proposal = r"(?:each|every|all).{0,40}propos\w*"
    assert re.search(rf"{proposal}.{{0,160}}(?:mark|label|tag|classif)"
                     rf"|(?:mark|label|tag|classif).{{0,160}}{proposal}",
                     prompt, re.I | re.S), "Mark each proposal delivered through fa orch request with its decision owner"
    for owner in (r"orchestrat\w*", r"(?:person|human|user)"):
        assert re.search(rf"{owner}.{{0,40}}decision|decision.{{0,40}}{owner}",
                         prompt, re.I | re.S), "Distinguish orchestrator decisions from person decisions"
    for category in (r"lane.{0,40}boundar|boundar.{0,40}lane", r"irreversible|hard.to.undo|difficult to undo",
                     r"\bscope\b", r"taste|preference"):
        assert re.search(category, prompt, re.I | re.S), f"Name this person-decision category: {category}"
    assert re.search(r"orchestrat.{0,140}(?:forward|refer|escalat|bring|ask|present).{0,180}(?:person|human|user)",
                     prompt, re.I | re.S), "The orchestrator forwards person decisions to the person"
    assert re.search(r"(?:forward|refer|escalat|bring|ask|present).{0,180}(?:multiple[ -]choice|choices|options)"
                     r"|(?:multiple[ -]choice|choices|options).{0,180}(?:forward|refer|escalat|bring|ask|present)",
                     prompt, re.I | re.S), "The orchestrator presents person decisions as multiple choice"
    evidence = r"(?:cit|refer|ground|support|back)\w*.{0,100}(?:facts?|numbers?|incidents?)"
    effect = r"expect\w*.{0,80}(?:measur|quantif)\w*.{0,80}(?:effect|impact|improv\w*|outcome|result)"
    assert re.search(rf"{proposal}.{{0,200}}{evidence}|{evidence}.{{0,200}}{proposal}",
                     prompt, re.I | re.S), "Each proposal cites facts numbers or a real incident"
    assert re.search(rf"{proposal}.{{0,240}}{effect}|{effect}.{{0,240}}{proposal}",
                     prompt, re.I | re.S), "Each proposal states its expected measurable effect"
    assert len(architect_events(case.store)) == 1, "Executing the child must not count as a second call"
    assert case.gh_calls == [] and case.pushes == []


def test_orch_architect_proposal_lands_in_orchestrator_inbox_without_publishing_orders(
    tmp_path, monkeypatch, capsys,
):
    from foragent.orch_inbox import questions, requests
    from foragent.state import StateStore

    case = architect_case(tmp_path, monkeypatch)
    case.propose = True
    assert inbox_command(case, "request", "--from", "planning", "--text", "Keep this planning request") == 0
    planning, = inbox_rows(case, capsys)
    assert inbox_rows(case, capsys, "questions") == []
    child = call_architect(case, capsys)
    finish_session(case, child)
    assert inbox_rows(case, capsys, "questions") == [], (
        "The architect call leaves forwarding person decisions to the orchestrator's later act")
    rows = inbox_rows(case, capsys)
    assert len(rows) == 2 and rows[0] == planning, "Use the same inbox as planning requests"
    proposal = rows[1]
    assert proposal["text"] == PROPOSAL and proposal["from"] == "architect" and proposal["status"] == "new"
    assert "Decision: person" in proposal["text"], "Preserve the architect's decision-owner mark in the durable request"
    # Read through the existing API on a fresh store, not a new private proposal queue.
    assert requests(StateStore(case.store.root)) == [planning, proposal]
    assert questions(StateStore(case.store.root), include_closed=True) == []
    assert not list((case.store.root / "approvals").glob("*.json")), "A proposal is not an approved order"
    assert not list((case.store.root / "runs").glob("*.json")), "Do not turn a proposal into lane work"
    assert case.store.local_issues() == [] and case.gh_calls == [] and case.pushes == []
    assert len(case.launches) == 1


def test_orch_architect_is_not_launched_by_ticks_merges_or_gate_paths(
    git_history, tmp_path, monkeypatch,
):
    from foragent import cli, executors
    from foragent.runner import run_issue
    from test_orch_review_handoff import bind_cli, make_case
    from test_tidy_cycle import record_finish

    case = make_case(git_history, tmp_path, monkeypatch, language="en", notify="none",
                     skill_libraries=[], test_command="true")
    case.main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "spawn_notify", lambda *a, **kw: None)
    case.gh_calls = record_gh(monkeypatch)
    popen = subprocess.Popen

    def local_checks_only(argv, *args, **kwargs):
        # Git stays inside git_history; the existing offline runner uses only
        # these inert Done-when checks. An independent agent launch must fail.
        assert isinstance(argv, (list, tuple)) and argv
        assert argv[0] == "git" or (list(argv[:2]) == ["/bin/sh", "-c"] and argv[2] in {
            "true", "test -f feature.txt", "test -f tests/acceptance/test_feature.py",
        }), f"Only fa orch architect --why may launch the architect, not a gate path: {argv}"
        return popen(argv, *args, **kwargs)

    def no_model_turn(*args, **kwargs):
        pytest.fail("Gate paths must leave the architect call to the orchestrator's judgment")

    monkeypatch.setattr(subprocess, "Popen", local_checks_only)
    for executor in (executors.ClaudeExecutor, executors.CodexExecutor):
        monkeypatch.setattr(executor, "run", no_model_turn)
    monkeypatch.setattr(executors, "executor_for", no_model_turn)

    # Exercise a real offline runner merge and its completion hooks, then cross
    # the old 30-merge tidy boundary. That signal must remain a request to judge.
    assert run_issue(1, case.ctx) == "merged"
    assert architect_events(case.store) == []
    for number in range(2, 32):
        record_finish(case, number)
        if number in (29, 30, 31):
            for flags in (("--no-start",),):
                assert cli_exit(case.main, ["gate", "tick", *flags,
                                           "--repo-root", str(case.root)]) == 0
                assert architect_events(case.store) == [], "No-start ticks and merge counts must not launch a role"
    assert cli_exit(case.main, ["gate", "status", "--json", "--repo-root", str(case.root)]) == 0
    assert architect_events(case.store) == []
    assert len(case.implementer.calls) == len(case.reviewer.calls) == 1, "Only the ordinary lane agents ran"
    assert case.gh_calls == []


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
