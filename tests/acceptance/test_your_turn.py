"""Acceptance test for your-turn. Locked by Foragent; do not edit."""

from dataclasses import replace
import json

from foragent import board, orch_inbox
from foragent.scheduler import tick
from test_board_status_groups import card_column, offline_github, seed_card  # noqa: F401
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


BACKLOG = "🗂 Backlog"
YOUR_TURN = "✋ Your turn"


def your_turn_case(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch, notify="none")
    case.store.finish_run(1, outcome="blocked")
    case.tracker.remove_labels(1, ["approved", "running"])
    board_id = board.ensure(case.tracker, "owner/repo")
    assert board_id, "The fake board must be configured"
    case.ctx.config = replace(case.config, board=board_id)
    seed_card(case.tracker, board_id, 1, BACKLOG)
    return case, bind_cli(case, monkeypatch), board_id


def ask_for_issues(main):
    return cli_exit(main, ["orch", "ask", "--question", "Which locale should we ship?",
                           "--option", "A", "--option", "B", "--recommend", "1",
                           "--issue", "167", "--issue", "170"])


def test_needs_human_issue_still_syncs_to_your_turn(git_history, tmp_path, monkeypatch):
    case, main, board_id = your_turn_case(git_history, tmp_path, monkeypatch)
    case.tracker.add_labels(1, ["needs-human"])

    assert tick(tick_context(case, start_runs=False)).skipped is None
    assert card_column(case.tracker, board_id, 1) == YOUR_TURN, (
        "An issue with needs-human must appear in Your turn"
    )


def test_unapproved_waiting_person_issue_syncs_to_your_turn(git_history, tmp_path, monkeypatch):
    case, main, board_id = your_turn_case(git_history, tmp_path, monkeypatch)
    case.tracker.add_labels(1, ["waiting-person"])
    assert "approved" not in case.tracker.get_issue(1).labels

    assert tick(tick_context(case, start_runs=False)).skipped is None
    assert card_column(case.tracker, board_id, 1) == YOUR_TURN, (
        "An unapproved waiting-person issue must appear in Your turn"
    )


def test_question_issues_sync_to_your_turn_until_answered(
    git_history, tmp_path, monkeypatch,
):
    case, main, board_id = your_turn_case(git_history, tmp_path, monkeypatch)
    for number in (167, 170, 171):
        case.tracker.add_issue(number, f"Unapproved issue {number}")
        seed_card(case.tracker, board_id, number, BACKLOG)
    assert tick(tick_context(case, start_runs=False)).skipped is None
    assert [card_column(case.tracker, board_id, number) for number in (167, 170, 171)] == [BACKLOG] * 3

    assert ask_for_issues(main) == 0, "fa orch ask must accept repeated --issue options"
    questions = orch_inbox.questions(case.store)
    assert len(questions) == 1
    question = questions[0]
    assert question["issues"] == [167, 170], "The question must durably retain both linked issues"
    assert question["options"] == ["A", "B"] and question["recommend"] == 1
    assert question["status"] == "open"

    assert tick(tick_context(case, start_runs=False)).skipped is None
    assert [card_column(case.tracker, board_id, number) for number in (167, 170)] == [YOUR_TURN] * 2, (
        "Every issue linked to an open question must appear in Your turn"
    )
    assert card_column(case.tracker, board_id, 171) == BACKLOG, "Unrelated issues must keep their group"

    assert cli_exit(main, ["orch", "answer", question["id"], "--text", "A"]) == 0
    assert orch_inbox.questions(case.store) == []
    closed = orch_inbox.questions(case.store, include_closed=True)
    assert len(closed) == 1 and closed[0]["status"] == "closed" and closed[0]["answer"] == "A"
    assert tick(tick_context(case, start_runs=False)).skipped is None
    assert [card_column(case.tracker, board_id, number) for number in (167, 170)] == [BACKLOG] * 2, (
        "Answering the question must return unapproved issues to Backlog on the next sync"
    )


def test_questions_json_lists_open_question_with_linked_issues(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main, board_id = your_turn_case(git_history, tmp_path, monkeypatch)
    for number in (167, 170):
        case.tracker.add_issue(number, f"Unapproved issue {number}")
    assert ask_for_issues(main) == 0, "fa orch ask must accept issue links for the JSON listing"
    capsys.readouterr()

    assert cli_exit(main, ["orch", "questions", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list) and len(rows) == 1, "questions --json must list the open question"
    assert rows[0]["question"] == "Which locale should we ship?"
    assert rows[0]["status"] == "open"
    assert rows[0].get("issues") == [167, 170], "questions --json must include the linked issues list"
