"""Order acceptance: the installed prompt hook delivers box facts to planning."""

from datetime import timedelta
from io import BytesIO, TextIOWrapper
import json
import sys

import pytest

from foragent import cli, orch_inbox, statusline
from test_fa_report_since import report_case  # noqa: F401
from test_presence_session_start import _cli_argv, session_case  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


def prompt(case, monkeypatch):
    settings = json.loads(statusline.claude_settings_path().read_text())
    output = []
    for group in settings.get("hooks", {}).get("UserPromptSubmit", []):
        for hook in group.get("hooks", []):
            argv = _cli_argv(hook.get("command", ""))
            if hook.get("type") != "command" or argv is None:
                continue
            payload = {"hook_event_name": "UserPromptSubmit", "session_id": "box-context",
                       "cwd": str(case.root), "prompt": "opaque user text"}
            streams = [TextIOWrapper(BytesIO(), encoding="utf-8") for _ in range(3)]
            stdin, stdout, stderr = streams
            stdin.write(json.dumps(payload))
            stdin.seek(0)
            with monkeypatch.context() as patch:
                patch.setattr(sys, "stdin", stdin)
                patch.setattr(sys, "stdout", stdout)
                patch.setattr(sys, "stderr", stderr)
                assert cli_exit(cli.main, argv) == 0
                stdout.flush()
                stderr.flush()
                assert stderr.buffer.getvalue() == b""
                raw = stdout.buffer.getvalue().decode()
            if raw.strip():
                value = json.loads(raw)
                context = value.get("hookSpecificOutput", {})
                assert context.get("hookEventName") == "UserPromptSubmit"
                output.append(context.get("additionalContext", ""))
    return "\n".join(output)


def stalled(case):
    now = case.clock[0]
    case.clock[0] -= timedelta(hours=3)
    row = orch_inbox.add_request(case.store, "architect", "Opaque request: Ζ42")
    orch_inbox.set_request_status(case.store, row["id"], "processing")
    case.clock[0] = now - timedelta(minutes=5)
    orch_inbox.set_request_status(case.store, row["id"], "processing", "Repeated note")
    case.clock[0] = now
    return row


def test_each_prompt_contains_current_stalls_and_questions_then_drops_completed_items(session_case, monkeypatch):
    case = session_case
    row = stalled(case)
    question = orch_inbox.ask(case.store, "Opaque choice: Ω89", ["Alpha", "Beta"], 1)
    for _ in range(2):
        context = prompt(case, monkeypatch)
        for value in (row["id"], row["text"], "3", question["id"], question["question"], "Alpha", "Beta"):
            assert value in context, f"Planning agent lacks box fact {value!r}"
    orch_inbox.set_request_status(case.store, row["id"], "done", "Published")
    orch_inbox.answer(case.store, question["id"], "Alpha")
    context = prompt(case, monkeypatch)
    assert row["text"] not in context and question["question"] not in context


@pytest.mark.parametrize("language,copy", [
    ("ko", "기획 세션에 물어볼 게 2개 있어요"),
    ("en", "2 questions need your answer in the planning session"),
])
def test_statusline_shows_only_person_questions_and_uses_localized_plain_copy(session_case, language, copy):
    case = session_case
    case.configure(language=language)
    stalled(case)
    orch_inbox.ask(case.store, "Another choice", ["yes", "later"], 1)
    # report_case already has one open question, so this is a fact count of two.
    output = case.status(session_id="not-started")
    assert copy in output
    assert "fa orch inbox" not in output and "Stuck requests" not in output and "멈춰" not in output


def test_zero_open_questions_has_no_planning_question_segment_even_with_stalls(session_case):
    case = session_case
    stalled(case)
    for row in orch_inbox.questions(case.store):
        orch_inbox.answer(case.store, row["id"], "Answered")
    output = case.status(session_id="not-started")
    assert "planning session" not in output and "fa orch inbox" not in output
    assert "Stuck requests" not in output


@pytest.mark.parametrize("role", ["orchestrator", "lane", ""])
def test_role_sessions_do_not_receive_planning_box_context(session_case, monkeypatch, role):
    case = session_case
    row = stalled(case)
    monkeypatch.setenv("FORAGENT_ROLE", role)
    assert row["text"] not in prompt(case, monkeypatch)


def test_gate_worktrees_and_exactly_sixty_minute_requests_do_not_receive_stalls(session_case, monkeypatch):
    case = session_case
    now = case.clock[0]
    case.clock[0] -= timedelta(hours=1)
    row = orch_inbox.add_request(case.store, "planning", "Exactly sixty minutes")
    orch_inbox.set_request_status(case.store, row["id"], "processing")
    case.clock[0] = now
    assert row["text"] not in prompt(case, monkeypatch)
    old = stalled(case)
    worktree = case.store.worktree_path(123)
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".foragent.yml").write_bytes((case.root / ".foragent.yml").read_bytes())
    case.root = worktree
    assert old["text"] not in prompt(case, monkeypatch)


@pytest.mark.parametrize("source", ["planning", "gate", "architect"])
def test_stalls_use_actual_transitions_and_clear_on_reentry(session_case, monkeypatch, source):
    case = session_case
    now = case.clock[0]
    case.clock[0] -= timedelta(hours=4)
    row = orch_inbox.add_request(case.store, source, "Opaque old request Ψ31")
    orch_inbox.set_request_status(case.store, row["id"], "processing")
    case.clock[0] = now
    assert row["text"] in prompt(case, monkeypatch)
    orch_inbox.set_request_status(case.store, row["id"], "done")
    orch_inbox.set_request_status(case.store, row["id"], "processing")
    assert row["text"] not in prompt(case, monkeypatch), "An actual transition restarts the age"
    case.clock[0] += timedelta(hours=1, seconds=1)
    assert row["text"] in prompt(case, monkeypatch)


@pytest.mark.parametrize("timestamp", ["broken", "2026-10-30T08:00:00", None, 17])
def test_malformed_timestamp_is_isolated_and_legacy_creation_is_used(session_case, monkeypatch, timestamp):
    case = session_case
    valid = stalled(case)
    bad = orch_inbox.add_request(case.store, "architect", "Malformed time must be skipped")
    path = case.store.root / "inbox/requests" / f"{bad['id']}.json"
    row = json.loads(path.read_text())
    row.update(status="processing", notes=[], created_at=timestamp)
    path.write_text(json.dumps(row))
    context = prompt(case, monkeypatch)
    assert valid["text"] in context and bad["text"] not in context
    row.update(created_at=(case.clock[0] - timedelta(hours=7)).isoformat(),
               updated_at=case.clock[0].isoformat())
    path.write_text(json.dumps(row))
    assert bad["text"] in prompt(case, monkeypatch), "Legacy age comes from creation, not updated_at"
