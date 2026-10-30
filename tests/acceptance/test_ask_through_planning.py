"""Acceptance test for ask-through-planning. Locked by Foragent; do not edit."""

from datetime import datetime, timedelta
from io import BytesIO, TextIOWrapper
import json
from pathlib import Path
import re
import sys

import pytest

from foragent import orch, orch_inbox
from test_fa_report_since import report_case  # noqa: F401 -- session_case dependency
from test_heartbeats import heartbeats_offline, heartbeats_write  # noqa: F401
from test_local_truth import record_gh
from test_orch_inbox import box_records, inbox_command, inbox_rows
from test_orch_review_handoff import bind_cli
from test_orch_seat import as_watcher
from test_orch_spawn_liveness import liveness_case  # noqa: F401
from test_presence_session_start import _cli_argv, session_case  # noqa: F401
from test_question_routing import routing_case
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_stop_whole_tree import fake_processes  # noqa: F401


@pytest.mark.parametrize("kind", [None, "question", "merge", "order"],
                         ids=["default-question", "question", "merge", "order"])
@pytest.mark.parametrize("age", [120, 121, None], ids=["fresh", "stale", "absent"])
def test_ask_kind_routes_to_planning_or_push_and_answer_closes_only_that_record(
    tmp_path, monkeypatch, capsys, kind, age,
):
    case = routing_case(tmp_path, monkeypatch)
    if age is not None:
        heartbeats_write(case.store, "plan", case.clock[0] - timedelta(seconds=age))
    # Keep another open item to prove that answering does not clear the box.
    other = orch_inbox.ask(case.store, "다음 우선순위는 무엇인가요?", ["API", "화면"], 1)
    question = "검증한 PR #50을 머지할까요?" if kind == "merge" else "이번 주문에 넣을까요?"
    options = ["진행해요", "나중에 해요"]
    flags = [] if kind is None else ["--kind", kind]
    capsys.readouterr()
    code = inbox_command(case, "ask", *flags, "--question", question,
                         "--option", options[0], "--option", options[1], "--recommend", "1")
    output = capsys.readouterr()
    assert code == 0, f"fa orch ask must accept kind {kind or 'question (default)'}: {output.err}"
    rows = inbox_rows(case, capsys, "questions")
    record = next(row for row in rows if row["question"] == question)
    assert record.get("kind") == (kind or "question"), "Persist kind; omitted --kind means question"
    assert record["status"] == "open" and record["answer"] is None
    assert record["options"] == options and record["recommend"] == 1
    saved = [row for _, row in box_records(case.store) if row.get("id") == record["id"]]
    assert len(saved) == 1 and saved[0]["kind"] == record["kind"]
    if age == 120:
        assert case.pushes == [], "A fresh planning heartbeat uses the session message path, without push"
        # The existing transport is agent-owned SendMessage (D76), not a Python
        # sender. Require an actionable handoff, rather than silently saving.
        handoff = output.out
        assert "planning" in handoff.lower(), "Fresh ask must surface its planning-session handoff"
        for value in (record["id"], question, *options):
            assert value in handoff, f"Planning handoff must carry {value!r}"
    else:
        assert len(case.pushes) == 1, "Absent or stale planning must receive exactly one fallback push"
        push = case.pushes[0]
        assert record["id"] in push["title"] + push["line"]
        assert question in push["title"] + push["line"]
    before_pushes = list(case.pushes)
    assert inbox_command(case, "answer", record["id"], "--text", options[0]) == 0
    assert [row["id"] for row in inbox_rows(case, capsys, "questions")] == [other["id"]]
    capsys.readouterr()
    assert inbox_command(case, "questions", "--all", "--json") == 0
    closed = next(row for row in json.loads(capsys.readouterr().out) if row["id"] == record["id"])
    assert closed["kind"] == record["kind"] and closed["status"] == "closed"
    assert closed["answer"] == options[0] and datetime.fromisoformat(closed["answered_at"]).tzinfo
    assert case.pushes == before_pushes and case.gh_calls == []


def refresh_planning_session(case, monkeypatch):
    """Run the recurring installed entry points, without inventing a hook CLI.

    Existing locked tests snapshot statusline reads as read-only. A
    UserPromptSubmit command installed by the same hook installer can renew
    planning without changing those reads or the session's start time.
    """
    from foragent import cli, statusline

    case.status()
    settings = json.loads(statusline.claude_settings_path().read_text())
    for group in settings.get("hooks", {}).get("UserPromptSubmit", []):
        for hook in group.get("hooks", []):
            argv = _cli_argv(hook.get("command", "")) if hook.get("type") == "command" else None
            if argv is None:
                continue
            payload = {"hook_event_name": "UserPromptSubmit", "session_id": "offline-session-A",
                       "cwd": str(case.root), "prompt": "진행 상황을 알려줘요"}
            stdin = TextIOWrapper(BytesIO(json.dumps(payload).encode()), encoding="utf-8")
            stdout, stderr = [TextIOWrapper(BytesIO(), encoding="utf-8") for _ in range(2)]
            with monkeypatch.context() as streams:
                streams.setattr(sys, "stdin", stdin)
                streams.setattr(sys, "stdout", stdout)
                streams.setattr(sys, "stderr", stderr)
                assert cli_exit(cli.main, argv) == 0, "The installed planning renewal hook must succeed"
                stderr.flush()
                assert stderr.buffer.getvalue() == b""


def test_planning_session_hook_renews_the_existing_120_second_heartbeat(session_case, monkeypatch):
    from foragent.cli import main

    case = session_case
    case.hook()
    assert cli_exit(main, ["plan", "heartbeat", "--repo-root", str(case.root)]) == 0
    path = orch_inbox.plan_heartbeat_path(case.store)
    for _ in range(2):
        case.clock[0] += timedelta(seconds=121)
        assert not orch_inbox.planning_alive(case.store), "Control: the old heartbeat has expired"
        refresh_planning_session(case, monkeypatch)
        assert orch_inbox.planning_alive(case.store), "Planning session hook must renew fa plan heartbeat"
        assert datetime.fromisoformat(json.loads(path.read_text())["at"]) == case.clock[0]
    assert case.gh_calls == []


@pytest.mark.parametrize("role", ["orchestrator", "architect", "worker", ""])
def test_role_session_hooks_never_renew_planning_heartbeat(session_case, monkeypatch, role):
    case = session_case
    path = heartbeats_write(case.store, "plan", case.clock[0] - timedelta(seconds=121))
    before = path.read_bytes()
    monkeypatch.setenv("FORAGENT_ROLE", role)
    case.hook()
    refresh_planning_session(case, monkeypatch)
    assert path.read_bytes() == before, "FORAGENT_ROLE set at all must not impersonate planning"
    assert not orch_inbox.planning_alive(case.store)


def test_gate_worktree_hooks_never_renew_planning_heartbeat(session_case, monkeypatch):
    case = session_case
    config = (case.root / ".foragent.yml").read_bytes()
    case.root = case.store.root / "worktrees/issue-50"
    case.root.mkdir(parents=True)
    (case.root / ".foragent.yml").write_bytes(config)
    monkeypatch.chdir(case.root)
    monkeypatch.delenv("FORAGENT_ROLE", raising=False)
    path = heartbeats_write(case.store, "plan", case.clock[0] - timedelta(seconds=121))
    before = path.read_bytes()
    case.hook()
    refresh_planning_session(case, monkeypatch)
    assert path.read_bytes() == before, "Gate worktrees must not impersonate the planning session"
    assert not orch_inbox.planning_alive(case.store)


def follow_with_injection(case, table, monkeypatch, capsys, inject, after_event=lambda: None):
    """Use the existing fake ancestors/seat and deterministic watch polling; no real wait."""
    as_watcher(case, table, monkeypatch)
    main = bind_cli(case, monkeypatch)
    gh_calls = record_gh(monkeypatch)
    argv = ["orch", "watch", "--interval", "1", "--repo-root", str(case.root)]
    assert cli_exit(main, argv) == 0
    capsys.readouterr()
    lines, polls = [], []

    def sleep(seconds):
        assert seconds == 1
        lines.extend(capsys.readouterr().out.splitlines())
        polls.append(seconds)
        if len(polls) == 1:
            inject(main)
            output = capsys.readouterr()
            assert output.err == ""
        elif len(polls) == 2:
            after_event()
        elif len(polls) == 4:
            raise KeyboardInterrupt
        case.clock[0] += timedelta(seconds=1)

    monkeypatch.setattr(orch.time, "sleep", sleep)
    assert cli_exit(main, [*argv, "--follow"]) == 0
    lines.extend(capsys.readouterr().out.splitlines())
    assert polls == [1] * 4
    assert gh_calls == [], "Inbox wake-ups must use local state, without GitHub"
    return lines


def id_lines(lines, record_id):
    return [line for line in lines if re.search(rf"(?<!\w){re.escape(record_id)}(?!\w)", line)]


def test_follow_prints_each_new_request_once_even_when_two_arrive_in_one_poll(
    liveness_case, fake_processes, monkeypatch, capsys,
):
    case, commands, pushes = liveness_case
    ids = []

    def inject(main):
        for _ in range(2):
            assert cli_exit(main, ["orch", "request", "--from", "planning", "--text", "r50 주문을 준비해요",
                                   "--repo-root", str(case.root)]) == 0
            ids.append(orch_inbox.requests(case.store)[-1]["id"])

    def progress():
        orch_inbox.set_request_status(case.store, ids[0], "processing", "범위를 확인했어요")

    lines = follow_with_injection(case, fake_processes, monkeypatch, capsys, inject, progress)
    assert len(set(ids)) == 2
    for record_id in ids:
        assert len(id_lines(lines, record_id)) == 1, f"watch --follow must print new request {record_id} once"
    assert len(lines) == 2, "One JSON line per new request; progress changes and unchanged polls stay quiet"
    assert all(isinstance(json.loads(line), dict) for line in lines)
    assert commands == pushes == [] and not fake_processes.signals


def test_follow_prints_a_new_answer_once_and_not_again_on_unchanged_polls(
    liveness_case, fake_processes, monkeypatch, capsys,
):
    case, commands, pushes = liveness_case
    question = orch_inbox.ask(case.store, "PR #50을 머지할까요?", ["Merge", "Later"], 1)

    def inject(main):
        assert cli_exit(main, ["orch", "answer", question["id"], "--text", "Merge",
                               "--repo-root", str(case.root)]) == 0

    lines = follow_with_injection(case, fake_processes, monkeypatch, capsys, inject)
    assert len(id_lines(lines, question["id"])) == 1, "watch --follow must print the newly answered question once"
    assert len(lines) == 1 and isinstance(json.loads(lines[0]), dict)
    assert commands == pushes == [] and not fake_processes.signals


def test_skill_and_cards_route_decisions_through_planning_and_keep_explicit_merge_choice():
    root = Path(orch.__file__).resolve().parents[2] / "skills/fa-orchestrator"
    skill = " ".join((root / "SKILL.md").read_text().split())
    assert re.search(r"AskUserQuestion.{0,200}only.{0,200}(?:own|orchestrator).{0,100}session", skill, re.I), (
        "AskUserQuestion must be limited to a person talking in the orchestrator's own session")
    assert re.search(r"(?:person|user|Jin).{0,100}(?:talk|spea|direct)|direct.{0,100}(?:person|user|Jin)", skill, re.I)
    assert "SendMessage" in skill, "Reuse the existing agent-owned planning cross-session transport"
    for name, kind in (("merge.md", "merge"), ("order-card.md", "order")):
        card = " ".join((root / "references" / name).read_text().split())
        assert re.search(rf"fa orch ask.{{0,100}}--kind {kind}\b", card), f"{name} must record kind {kind}"
        assert "planning" in card.lower() and "fa orch answer" in card
    merge = " ".join((root / "references/merge.md").read_text().split())
    assert re.search(r"(?:person|user).{0,120}(?:chose|chooses|choice|explicit)", merge, re.I), (
        "The person's explicit Merge choice remains required")
    assert merge.index("fa orch answer") < merge.index("fa orch merge"), (
        "Record the person's answer before fa orch merge; routing alone is not permission")
