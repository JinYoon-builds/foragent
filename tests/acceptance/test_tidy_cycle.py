"""Acceptance test for tidy-cycle. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
import os
import re
import subprocess

import pytest

from test_orch_inbox import inbox_command, inbox_rows
from test_order_progress import offline_local_cli
from test_skills_autosync import cli_exit, isolated_home
from test_usage_read import usage_event


@pytest.fixture(autouse=True)
def no_tidy_signals_or_model_turns(monkeypatch):
    from foragent import executors

    def forbidden(*args, **kwargs):
        pytest.fail("Tidy scheduling must not signal real processes or start a real model turn")

    popen = subprocess.Popen

    def local_checks_only(argv, *args, **kwargs):
        # Real Git operates only on git_history. The runner's fake implementer
        # needs just these inert Done-when checks; every launcher stays blocked.
        assert isinstance(argv, (list, tuple)) and argv
        assert argv[0] == "git" or (list(argv[:2]) == ["/bin/sh", "-c"] and argv[2] in {
            "true", "test -f feature.txt", "test -f tests/acceptance/test_feature.py",
        }), f"Unexpected process launch in tidy scheduling: {argv}"
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_checks_only)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")


def tidy_command(case, *args):
    return cli_exit(case.main, ["tidy", *args, "--repo-root", str(case.root)])


def due(case, capsys):
    capsys.readouterr()
    assert tidy_command(case, "due", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert {"due", "reason", "merges_since", "last_tidy_at"} <= result.keys()
    assert type(result["due"]) is bool and type(result["merges_since"]) is int
    assert result["reason"] in ("merges", "idle-quota", None)
    return result


def record_finish(case, number, *, outcome="merged"):
    if hasattr(case, "clock"):
        case.clock[0] += timedelta(seconds=1)
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    case.store.finish_run(number, outcome=outcome)


def tidy_gate_case(git_history, tmp_path, monkeypatch):
    from foragent import cli
    from foragent.state import StateStore
    from test_local_truth import record_gh
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch, language="en", notify="none",
                     skill_libraries=[], test_command="true")
    # Start with an idle gate, without counting make_case's initial fixture run.
    case.clock[0] = datetime.now(UTC)
    case.store = StateStore(tmp_path / "tidy-state", now=lambda: case.clock[0])
    case.ctx.store = case.store
    case.tracker.close_issue(1)
    case.main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "spawn_notify", lambda *a, **kw: None)
    case.gh_calls = record_gh(monkeypatch)
    return case


def tidy_tick(case):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    ctx = tick_context(case, start_runs=False)
    launches = []
    ctx.launcher = launches.append  # Fake only, even if the gate tries to dispatch work.
    approvals = {path: path.read_bytes() for path in (case.store.root / "approvals").glob("*.json")}
    issues = set(case.tracker.issues)
    tick(ctx)
    assert launches == [], "A due schedule is a request for judgment, not permission to launch tidy work"
    assert set(case.tracker.issues) == issues, "The gate must not publish a tidy order itself"
    assert {path: path.read_bytes() for path in (case.store.root / "approvals").glob("*.json")} == approvals
    assert case.implementer.calls == case.reviewer.calls == [], "Scheduling must not call an agent"


def tidy_requests(case, capsys):
    return [row for row in inbox_rows(case, capsys) if "tidy due" in row["text"].lower()]


def assert_tidy_evidence(request, *, reason, merges):
    text = request["text"].lower()
    assert request["from"] == "gate" and request["status"] == "new"
    assert reason in text
    assert re.search(rf"merges?(?:_since)?\D{{0,40}}\b{merges}\b|\b{merges}\b.{{0,40}}merges?", text), (
        "Include the measured merge count in the request, not only a generic tidy reminder")


def test_tidy_cycle_thirty_merges_trigger_and_done_resets_persistently(git_history, tmp_path, monkeypatch, capsys):
    from foragent.state import StateStore

    case = tidy_gate_case(git_history, tmp_path, monkeypatch)
    # Non-merge outcomes must not advance the threshold.
    record_finish(case, 100, outcome="blocked")
    for number in range(1, 30):
        record_finish(case, number)
    tidy_tick(case)
    assert tidy_requests(case, capsys) == []
    assert due(case, capsys) == {"due": False, "reason": None, "merges_since": 29, "last_tidy_at": None}
    record_finish(case, 30)
    tidy_tick(case)
    request, = tidy_requests(case, capsys)
    assert_tidy_evidence(request, reason="merges", merges=30)
    assert due(case, capsys) == {"due": True, "reason": "merges", "merges_since": 30, "last_tidy_at": None}
    before = case.clock[0]
    assert tidy_command(case, "done") == 0
    # A new CLI invocation opens a fresh StateStore through for_repo().
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    after = due(case, capsys)
    assert after["due"] is False and after["reason"] is None and after["merges_since"] == 0
    assert before <= datetime.fromisoformat(after["last_tidy_at"]) <= case.clock[0]
    record_finish(case, 31)
    tidy_tick(case)
    assert due(case, capsys)["merges_since"] == 1
    assert tidy_requests(case, capsys) == [request], "Keep the old request as history, without a new reminder yet"
    assert case.gh_calls == []


def test_tidy_cycle_idle_quota_uses_recorded_executor_usage_and_active_lanes(git_history, tmp_path, monkeypatch, capsys):
    from foragent.executors import ExecResult
    from foragent.runner import run_issue
    from test_local_view import local_cli, record_gh
    from test_orch_review_handoff import make_case

    case = make_case(git_history, tmp_path, monkeypatch, notify="none", skill_libraries=[], test_command="true")
    calls = record_gh(monkeypatch)

    def execute(**kwargs):
        case.implement(kwargs)
        log = kwargs["log_path"]
        log.parent.mkdir(parents=True, exist_ok=True)
        # Use the existing executor-event fixture and the real runner ingestion path.
        event = usage_event("claude", used_percent=40)
        resets = int((case.clock[0] + timedelta(hours=5)).timestamp())
        event["rate_limit_info"]["resetsAt"] = resets
        for window in event["rate_limit_info"]["unifiedWindows"].values():
            window["resetsAt"] = resets
        with log.open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        return ExecResult(0, False, "Implemented", log)

    monkeypatch.setattr(case.implementer, "run", execute)
    assert run_issue(1, case.ctx) == "merged"
    # The schedule uses the recorded usage produced above, without asking a model
    # to classify whether an idle quota is a good time to perform tidy work.
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    tick(tick_context(case, start_runs=False))
    # local_cli's no-polling guard applies to observation, after runner polling ends.
    case.main = local_cli(case, monkeypatch)
    idle = due(case, capsys)
    assert idle["due"] is True and idle["reason"] == "idle-quota" and idle["merges_since"] == 1
    request, = tidy_requests(case, capsys)
    assert_tidy_evidence(request, reason="idle-quota", merges=1)
    assert re.search(r"\b(?:40|60)\s*%|(?:used|remaining|percent).{0,30}\b(?:40|60)\b", request["text"], re.I), (
        "Include the recorded 40 % usage (or 60 % remaining) as quota evidence")
    case.store.start_run(2, branch="foragent/issue-2", worktree=case.store.worktree_path(2), lane="main")
    busy = due(case, capsys)
    assert busy["due"] is False and busy["reason"] is None
    assert tidy_requests(case, capsys) == [request]
    assert calls == []


def test_tidy_cycle_no_merges_and_no_usage_are_not_a_reason_to_tidy(tmp_path, monkeypatch, capsys):
    case = offline_local_cli(tmp_path, monkeypatch)
    assert due(case, capsys) == {"due": False, "reason": None, "merges_since": 0, "last_tidy_at": None}
    assert tidy_requests(case, capsys) == []
    assert case.gh_calls == []


def test_tidy_cycle_skill_reads_widely_prioritizes_and_proposes_owned_lane_work():
    text = (Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md").read_text().lower()
    section = re.search(r"(?ms)^#{1,4}[^\n]*(?:tidy|정리)[^\n]*\n(.*?)(?=^#{1,4} |\Z)", text)
    assert section, "The orchestrator needs an actionable tidy section (D68)"
    tidy = " ".join(section[1].split())
    for command in ("fa tidy due", "fa tidy done", "fa orch inbox", "fa orch architect --why"):
        assert command in tidy, f"The tidy cycle needs the evidence/request/architect tool: {command}"
    assert "tidy due" in tidy and re.search(r"evidence|numbers|measure", tidy)
    assert re.search(r"architect.{0,160}(?:read|inspect|refactor|propos)|(?:read|inspect|refactor|propos).{0,160}architect", tidy), (
        "Delegate structural inspection and proposals to the architect")
    assert re.search(r"(?:decid|judg|whether).{0,160}(?:architect|call)|(?:architect|call).{0,160}(?:decid|judg|whether)", tidy), (
        "The request asks for the orchestrator's judgment about calling the architect")
    assert "fa orch ask" in tidy or "fa orch questions" in tidy, "Proposals belong in the person's decision list"
    assert re.search(r"(?:propose|proposal).{0,160}(?:order|tidy)|(?:order|tidy).{0,160}(?:propose|proposal)", tidy)
    assert re.search(r"(?:human|person|user).{0,100}approv|approv.{0,100}(?:human|person|user)", tidy)
    assert re.search(r"lanes?.{0,120}(?:own|responsible)|(?:own|responsible).{0,120}lanes?", tidy)
    assert re.search(r"(?:all|every).{0,40}existing tests.{0,60}pass", tidy)
    assert re.search(r"behaviou?r.{0,50}(?:unchanged|same)|(?:unchanged|same).{0,50}behaviou?r", tidy)


def test_tidy_cycle_due_posts_exactly_one_inbox_request_per_due_period(git_history, tmp_path, monkeypatch, capsys):
    from foragent.state import StateStore

    case = tidy_gate_case(git_history, tmp_path, monkeypatch)
    assert inbox_command(case, "request", "--from", "planning", "--text", "Keep this unrelated request") == 0
    unrelated, = inbox_rows(case, capsys)
    for number in range(1, 31):
        record_finish(case, number)
    for _ in range(3):
        case.store = case.ctx.store = StateStore(case.store.root, now=lambda: case.clock[0])
        tidy_tick(case)
    requests = tidy_requests(case, capsys)
    assert len(requests) == 1, "30 merges must create exactly one 'tidy due' request in fa orch inbox"
    first, = requests
    assert_tidy_evidence(first, reason="merges", merges=30)
    assert inbox_command(case, "request-status", first["id"], "done", "--note", "Evidence read") == 0
    for number in range(31, 36):
        record_finish(case, number)
    case.clock[0] += timedelta(minutes=5)
    tidy_tick(case)
    rows = tidy_requests(case, capsys)
    assert len(rows) == 1 and rows[0]["id"] == first["id"], (
        "Reading/closing a request, more merges, ticks and store restarts must not re-post the same due period")
    assert tidy_command(case, "done") == 0  # Only completed tidy work starts a new counting period.
    for number in range(36, 65):
        record_finish(case, number)
    tidy_tick(case)
    assert len(tidy_requests(case, capsys)) == 1, "29 merges in the next period are not due"
    record_finish(case, 65)
    tidy_tick(case)
    rows = tidy_requests(case, capsys)
    assert len(rows) == 2 and rows[1]["id"] != first["id"]
    assert_tidy_evidence(rows[1], reason="merges", merges=30)
    assert unrelated in inbox_rows(case, capsys), "Tidy scheduling must preserve other inbox requests"
    assert case.gh_calls == []
