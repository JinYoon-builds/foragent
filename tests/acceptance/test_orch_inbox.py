"""Acceptance test for orch-inbox. Locked by Foragent; do not edit."""

import json
import re

from test_order_progress import offline_local_cli
from test_skills_autosync import cli_exit, isolated_home
from test_squirrel_running import view_offline


def inbox_case(tmp_path, monkeypatch):
    case = offline_local_cli(tmp_path, monkeypatch)
    (case.root / ".foragent.yml").write_text(
        "repo: owner/repo\nlanguage: ko\nskill_libraries: []\nnotify: none\n",
        encoding="utf-8",
    )
    return case


def inbox_command(case, *args):
    return cli_exit(case.main, ["orch", *args, "--repo-root", str(case.root)])


def inbox_rows(case, capsys, kind="inbox"):
    capsys.readouterr()
    assert inbox_command(case, kind, "--json") == 0, f"fa orch {kind} --json must exist"
    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list), "the request/question listing must be a JSON array"
    return rows


def add_request(case, capsys, text):
    assert inbox_command(case, "request", "--from", "planning", "--text", text) == 0, (
        "fa orch request must durably accept a planning request"
    )
    rows = [row for row in inbox_rows(case, capsys) if row["text"] == text]
    assert rows
    return rows[-1]


def add_question(case, capsys, question):
    assert inbox_command(case, "ask", "--question", question,
                         "--option", "이번 주문에 넣어요", "--option", "다음에 해요",
                         "--recommend", "1") == 0, "fa orch ask must persist the human question"
    rows = [row for row in inbox_rows(case, capsys, "questions") if row["question"] == question]
    assert len(rows) == 1
    return rows[0]


def box_records(store):
    """Inspect durable records without prescribing the box directory or filenames."""
    return [(path, value) for path in store.root.rglob("*.json")
            if isinstance(value := json.loads(path.read_text(encoding="utf-8")), dict)]


def test_orch_inbox_requests_have_distinct_files_and_status_notes_survive_reopening(
    tmp_path, monkeypatch, capsys,
):
    from foragent.state import StateStore

    case = inbox_case(tmp_path, monkeypatch)
    text = "D75 주문을 만들어요\n결정 D76도 반영해요."
    for _ in range(2):
        assert inbox_command(case, "request", "--from", "planning", "--text", text) == 0
    rows = inbox_rows(case, capsys)
    assert len(rows) == 2 and len({row["id"] for row in rows}) == 2
    paths = []
    for row in rows:
        assert row["id"] and row["text"] == text and row["status"] == "new"
        matches = [(path, record) for path, record in box_records(StateStore(case.store.root))
                   if record.get("id") == row["id"] and record.get("text") == text]
        assert len(matches) == 1, "each request must have its own durable file"
        assert matches[0][1]["status"] == "new"
        paths.append(matches[0][0])
    assert len(set(paths)) == 2
    request_id = rows[0]["id"]
    for status, note in (("processing", "주문 범위를 정리했어요"), ("done", "발행까지 끝냈어요")):
        assert inbox_command(case, "request-status", str(request_id), status, "--note", note) == 0
        updated = {row["id"]: row for row in inbox_rows(case, capsys)}
        assert updated[request_id]["status"] == status
        assert updated[rows[1]["id"]]["status"] == "new"
        records = [record for _, record in box_records(StateStore(case.store.root))
                   if record.get("id") == request_id and record.get("text") == text]
        assert len(records) == 1 and records[0]["status"] == status
        assert note in json.dumps(records[0], ensure_ascii=False)
    assert case.gh_calls == []


def test_orch_inbox_questions_preserve_choices_and_answer_closes_only_its_question(
    tmp_path, monkeypatch, capsys,
):
    from foragent.state import StateStore

    case = inbox_case(tmp_path, monkeypatch)
    first = add_question(case, capsys, "이번 주문에 디자인도 넣을까요?")
    second = add_question(case, capsys, "다음 우선순위는 무엇인가요?")
    for row in inbox_rows(case, capsys, "questions"):
        assert row["id"] and row["status"] == "open"
        assert row["options"] == ["이번 주문에 넣어요", "다음에 해요"]
        assert row["recommend"] == 1
        records = [record for _, record in box_records(StateStore(case.store.root))
                   if record.get("id") == row["id"] and record.get("question") == row["question"]]
        assert len(records) == 1 and records[0]["status"] == "open"
    answer = "이번엔 포함하고 접근성도 살펴봐 주세요."
    assert inbox_command(case, "answer", str(first["id"]), "--text", answer) == 0
    assert [row["id"] for row in inbox_rows(case, capsys, "questions")] == [second["id"]]
    records = [record for _, record in box_records(StateStore(case.store.root))
               if record.get("id") == first["id"] and record.get("question") == first["question"]]
    assert len(records) == 1 and records[0]["status"] == "closed"
    assert records[0]["answer"] == answer
    assert case.gh_calls == []


def test_orch_inbox_unknown_ids_fail_without_creating_requests_or_questions(tmp_path, monkeypatch, capsys):
    case = inbox_case(tmp_path, monkeypatch)
    # Establish working verbs first, so an absent parser cannot satisfy this edge.
    request = add_request(case, capsys, "기존 요청")
    question = add_question(case, capsys, "무엇을 먼저 할까요?")
    before = {path: path.read_bytes() for path, _ in box_records(case.store)}
    for args in (("request-status", "missing-request", "processing"),
                 ("request-status", "missing-request", "done", "--note", "끝"),
                 ("answer", "missing-question", "--text", "답")):
        assert inbox_command(case, *args) != 0
    assert inbox_rows(case, capsys) == [request]
    assert inbox_rows(case, capsys, "questions") == [question]
    assert {path: path.read_bytes() for path, _ in box_records(case.store)} == before
    assert case.gh_calls == []


def test_orch_inbox_skill_reads_box_on_start_and_restart_and_records_messages_under_d71():
    from test_orch_ask_rule import ask_rule_text

    text = ask_rule_text()
    for command in ("fa orch inbox", "fa orch request", "fa orch ask", "fa orch questions"):
        assert command in text, f"the orchestrator needs the durable box tool: {command}"
    assert re.search(r"(?:start|restart|new session).{0,180}(?:first|before).{0,120}(?:inbox|request box)"
                     r"|(?:first|before).{0,120}(?:inbox|request box).{0,180}(?:start|restart|new session)", text)
    assert "restart" in text
    assert re.search(r"cross.session|session.to.session|세션 간", text)
    assert re.search(r"(?:every|all|each).{0,80}(?:request|question)", text)
    assert re.search(r"(?:even|also|always).{0,180}(?:box|file|persist|message)", text)
    assert "d71" in text and re.search(r"only.{0,100}(?:ask|question)|(?:ask|question).{0,100}only", text)
    for kind in (r"goal.{0,40}chang|chang.{0,40}goal", r"trade.off", r"taste|preference",
                 r"hard.to.undo|irreversible|difficult to undo", r"scope", r"priorit"):
        assert re.search(kind, text), kind
