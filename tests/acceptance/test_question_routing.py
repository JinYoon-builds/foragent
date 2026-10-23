"""Acceptance test for question-routing. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
import json
import re

from test_heartbeats import heartbeats_write
from test_orch_inbox import add_question, inbox_case, inbox_command, inbox_rows
from test_skills_autosync import cli_exit, isolated_home
from test_squirrel_running import view_offline


def routing_case(tmp_path, monkeypatch):
    from foragent import cli, notify, orch
    from foragent.state import StateStore

    case = inbox_case(tmp_path, monkeypatch)
    (case.root / ".foragent.yml").write_text(
        "repo: owner/repo\nlanguage: ko\nskill_libraries: []\nnotify: command:/offline/push\n",
        encoding="utf-8",
    )
    case.clock = [datetime.now(UTC)]
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kw: cls(case.store.root, now=lambda: case.clock[0])))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.clock[0] if tz else case.clock[0].replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(orch, "datetime", Clock)
    case.pushes = []

    def push(config, title, line, link=None):
        case.pushes.append({"config": config, "title": title, "line": line, "link": link})
        return True

    original = notify.send
    for module in (notify, cli, orch):
        for name, value in tuple(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, name, push)
    return case


def test_question_routing_absent_planning_session_pushes_once_for_each_question(tmp_path, monkeypatch, capsys):
    case = routing_case(tmp_path, monkeypatch)
    for question in ("이번 주문 범위를 넓힐까요?", "다음엔 무엇을 먼저 할까요?"):
        previous = len(case.pushes)
        add_question(case, capsys, question)
        assert len(case.pushes) == previous + 1
        push = case.pushes[-1]
        assert push["config"].notify == "command:/offline/push"
        assert question in push["title"] + push["line"]
    for _ in range(3):
        assert len(inbox_rows(case, capsys, "questions")) == 2
    assert len(case.pushes) == 2 and case.gh_calls == []


def test_question_routing_planning_heartbeat_is_alive_through_120_seconds_and_can_be_renewed(
    tmp_path, monkeypatch, capsys,
):
    case = routing_case(tmp_path, monkeypatch)
    command = ["plan", "heartbeat", "--repo-root", str(case.root)]
    assert cli_exit(case.main, command) == 0, "fa plan heartbeat must record the planning session"
    path = case.store.root / "plan-heartbeat.json"
    assert path.is_file() and datetime.fromisoformat(json.loads(path.read_text())["at"]) == case.clock[0]
    for age, expected_pushes in ((0, 0), (120, 0), (121, 1)):
        heartbeats_write(case.store, "plan", case.clock[0] - timedelta(seconds=age))
        add_question(case, capsys, f"{age}초 시점: 우선순위를 바꿀까요?")
        assert len(case.pushes) == expected_pushes, f"planning heartbeat age {age}s"
    assert cli_exit(case.main, command) == 0
    assert datetime.fromisoformat(json.loads(path.read_text())["at"]) == case.clock[0]
    add_question(case, capsys, "다시 열린 기획 세션에 물어요")
    assert len(case.pushes) == 1 and case.gh_calls == []


def test_question_routing_brief_counts_open_questions_and_korean_line_disappears_at_zero(
    tmp_path, monkeypatch, capsys,
):
    case = routing_case(tmp_path, monkeypatch)

    def brief(*flags):
        capsys.readouterr()
        assert cli_exit(case.main, ["brief", *flags, "--repo-root", str(case.root)]) == 0, (
            "fa brief is the planning briefing entry point (#113)"
        )
        return capsys.readouterr().out

    # Check the briefing entry point before creating questions: a missing #113
    # must fail as a missing brief, rather than as an unrelated ask failure.
    assert json.loads(brief("--json"))["questions_open"] == 0
    assert "답할 질문" not in brief()
    first = add_question(case, capsys, "디자인을 이번에 할까요?")
    second = add_question(case, capsys, "범위를 더 넓힐까요?")
    assert json.loads(brief("--json"))["questions_open"] == 2
    assert "답할 질문 2개" in brief()
    assert inbox_command(case, "answer", str(first["id"]), "--text", "넣어요") == 0
    assert json.loads(brief("--json"))["questions_open"] == 1
    assert "답할 질문 1개" in brief()
    assert inbox_command(case, "answer", str(second["id"]), "--text", "지금 범위로 해요") == 0
    assert json.loads(brief("--json"))["questions_open"] == 0
    assert "답할 질문" not in brief()
    assert len(case.pushes) == 2 and case.gh_calls == []


def test_question_routing_skill_messages_live_planning_session_and_pushes_only_when_absent():
    from test_orch_ask_rule import ask_rule_text

    text = ask_rule_text()
    assert "planning" in text and "heartbeat" in text
    assert "120" in text and "fa orch ask" in text
    assert re.search(r"(?:alive|fresh).{0,240}(?:cross.session|session.to.session|message)", text)
    assert re.search(r"(?:alive|fresh).{0,240}(?:no push|do not.{0,30}(?:push|notif)|without.{0,30}(?:push|notif))", text)
    assert re.search(r"(?:absent|not alive|closed|stale|no fresh).{0,240}(?:push|notif)", text)
