"""Acceptance for the #1160 edit: questions show whose turn it is, without a hardcoded name.

Locked by Foragent; do not edit.

No person's name is built into the screen. In Korean with no configured ``person_name`` a
question on the person's turn reads "질문" alone (as in "#2158 질문") and the approval wait
reads "승인 기다리는 중". The configured-name behaviour is covered by
tests/acceptance/test_question_recipient.py, which configures 진님 for Korean.

Every check goes through public entry points: the real config loader, ``fa orch ask``,
``fa order progress``, the needs-human label on the tracker, the lane view frame (acorns,
header, dashboard bubble, the bubble of a clicked acorn, preparation rows) and the
``fa statusline`` command. tests/acceptance/test_question_recipient.py keeps its own fixture
(person_name 진님 for Korean); this file only reuses its seeding and rendering helpers.
"""

from datetime import UTC, datetime
import re

import pytest
import yaml

from foragent.config import load_config
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from test_order_progress import offline_local_cli
from test_question_recipient import ask_person, render, seed_spec_question
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401
from test_squirrel_running import plain_frame, utf8_terminal, view_offline  # noqa: F401

PERSON, ORCH = 1059, 1060
ORCHESTRATOR = {"ko": "오케스트레이터", "en": "the orchestrator"}
ROUTES = ["inbox", "needs-human"]
TITLE = "로그인 준비"
CELL_ASK = "어느 지역에 저장할까요?"
# The header and dashboard bubble sentences that count the person's questions.
ASKS = {"ko": r"답할 질문"}


def make_case(tmp_path, monkeypatch, language):
    """The question_recipient fixture for one language, with no person_name configured."""
    from foragent import statusline

    tmp_path.mkdir(parents=True, exist_ok=True)
    case = offline_local_cli(tmp_path, monkeypatch)
    case.language = language
    settings = {
        "repo": "owner/repo", "language": language, "skill_libraries": [],
        "notify": "none", "lanes": {name: {"paths": [f"{name}/**"]} for name in ("person", "spec")},
    }
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
    case.config = load_config(case.root)
    case.clock = [datetime(2026, 10, 31, 12, tzinfo=UTC)]
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.tracker = FakeTracker(now=lambda: case.clock[0])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return case.clock[0].astimezone(tz) if tz else case.clock[0].replace(tzinfo=None)

    monkeypatch.setattr(statusline, "datetime", Clock)
    monkeypatch.delenv(statusline.NESTED_ENV, raising=False)
    return case


def progress(case, *args):
    assert cli_exit(case.main, ["order", "progress", *args, "--repo-root", str(case.root)]) == 0, args


def seed_preparation(case, step):
    """An order in preparation at ``step`` ("chunk" leaves one cell question for the person)."""
    progress(case, "start", "--title", TITLE)
    if step == "chunk":
        progress(case, "chunk", "--add", "Login")
        progress(case, "cell", "--chunk", "Login", "--slot", "goal", "--ask", CELL_ASK)
    else:
        progress(case, step)


def seed(case, capsys, route, prep="approval"):
    """A person question (inbox or needs-human), an orchestrator question and a preparation."""
    seed_spec_question(case, PERSON, "person")
    seed_spec_question(case, ORCH, "spec")
    if route == "inbox":
        ask_person(case, capsys, PERSON)
    else:
        case.tracker.add_labels(PERSON, ["needs-human"])
    seed_preparation(case, prep)


def names_someone(text):
    """True when the text addresses a person by a built-in word: 진님, any ~님 honorific, or "you"."""
    return "진님" in text or re.search(r"\w님", text) is not None or re.search(r"\byou\b", text.casefold()) is not None


def clicked(case, number):
    """The lane view with the acorn of ``number`` clicked: its bubble explains the question."""
    from foragent import status, watch

    case.store.refresh_local_issues(case.tracker.list_issues("all"), [], case.config.lanes, case.clock[0])
    payload = status.collect(case.store, case.config, case.clock[0])
    snapshot = watch.collect(payload, case.tracker, case.store, case.config, case.clock[0])
    return plain_frame(watch.render_frame(snapshot, case.config, case.root, width=180, now=case.clock[0],
                                          state={"explanation_target": ("acorn", number)}))


def acorn_words(text, number):
    """The words drawn right after ``#number`` on an acorn, up to the acorn's end or a separator."""
    words = []
    for line in text.splitlines():
        for match in re.finditer(rf"#{number}\b(.*)", line):
            tail = re.split(r"[▌▐◗◖]|·| {2,}|\|", match.group(1))[0]
            words.append(tail.strip())
    return words


def segments(line):
    return [part.strip() for part in line.split("·")]


def prep_lines(view):
    return [line for line in view.splitlines() if TITLE in line or CELL_ASK in line or "승인" in line
            or "approv" in line.casefold()]


def bubble_rows(text):
    """Rows inside the dashboard bubble's box (between its │/┤ borders)."""
    rows = []
    for line in text.splitlines():
        found = re.search(r"[│┤](.*)│\s*$", line)
        if found:
            rows.append(found.group(1).strip())
    return [row for row in rows if row]


def clicked_rows(case, number):
    rows = bubble_rows(clicked(case, number))
    assert any(f"#{number}" in row for row in rows), f"The clicked bubble must explain #{number}: {rows}"
    return rows


# Statement 1: no person_name -> the Korean screen names nobody; "질문" and "승인 기다리는 중" alone ---

@pytest.mark.parametrize("route", ROUTES)
def test_unnamed_korean_question_reads_question_alone(
    tmp_path, monkeypatch, capsys, isolated_home, view_offline, utf8_terminal, route,
):
    """Reject a built-in name or any stand-in word after "질문" on the person's acorn."""
    case = make_case(tmp_path, monkeypatch, "ko")
    seed(case, capsys, route)
    view = render(case, "lane-view", monkeypatch)
    words = acorn_words(view, PERSON)
    assert words, f"The person's question must be shown on its acorn: {view}"
    assert "질문" in words, f"The person's question must read '질문' alone: {words}"
    assert not any(word.startswith("질문") and word != "질문" for word in words), (
        f"Nothing follows '질문' on the person's acorn: {words}")
    orchestrator = acorn_words(view, ORCH)
    assert any(ORCHESTRATOR["ko"] in word for word in orchestrator), (
        f"The orchestrator's question still names the orchestrator: {orchestrator}")
    assert not names_someone(view), f"With no person_name the lane view names nobody: {view}"


@pytest.mark.parametrize("route", ROUTES)
def test_unnamed_korean_approval_wait_reads_waiting_for_approval_alone(
    tmp_path, monkeypatch, capsys, isolated_home, view_offline, utf8_terminal, route,
):
    """Reject a built-in name before "승인 기다리는 중", or the wait dropped or repeated."""
    case = make_case(tmp_path, monkeypatch, "ko")
    seed(case, capsys, route)
    prep = prep_lines(render(case, "lane-view", monkeypatch))
    assert any(TITLE in line for line in prep), f"The order preparation must be shown: {prep}"
    waits = [part for line in prep for part in segments(line) if "승인" in part]
    assert waits == ["승인 기다리는 중"], f"The approval wait must read '승인 기다리는 중' once and alone: {prep}"


def test_unnamed_korean_preparation_question_names_nobody(
    tmp_path, monkeypatch, capsys, isolated_home, view_offline, utf8_terminal,
):
    """Reject the hardcoded name in front of an order-interview question in preparation."""
    case = make_case(tmp_path, monkeypatch, "ko")
    seed(case, capsys, "inbox", prep="chunk")
    prep = prep_lines(render(case, "lane-view", monkeypatch))
    asked = [line for line in prep if CELL_ASK in line]
    assert len(asked) == 1, f"The person's preparation question is shown once: {prep}"
    assert not names_someone(asked[0]), f"With no person_name the preparation names nobody: {asked}"


@pytest.mark.parametrize("route", ROUTES)
def test_unnamed_korean_bubbles_and_statusline_name_nobody(
    tmp_path, monkeypatch, capsys, isolated_home, view_offline, utf8_terminal, route,
):
    """Reject the hardcoded name in the header, the dashboard bubble, the clicked bubble or the statusline."""
    case = make_case(tmp_path, monkeypatch, "ko")
    seed(case, capsys, route)
    view = render(case, "lane-view", monkeypatch)
    asks = [line for line in view.splitlines() if re.search(ASKS["ko"], line)]
    assert asks, f"The header and bubble must still count the person's questions: {view}"
    rows = clicked_rows(case, PERSON)
    assert any("물어" in row or "질문" in row for row in rows), (
        f"The clicked bubble still says the person will be asked: {rows}")
    statusline = render(case, "statusline", monkeypatch)
    assert "질문" in statusline and ORCHESTRATOR["ko"] in statusline, (
        f"The statusline still groups questions by turn: {statusline}")
    for surface, text in {"header and bubble": "\n".join(asks), "clicked bubble": "\n".join(rows),
                          "statusline": statusline}.items():
        assert not names_someone(text), f"With no person_name the {surface} names nobody: {text}"
