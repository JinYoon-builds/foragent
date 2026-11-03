"""Locked contract: FA owns interview progress; reopening briefs the next PM."""

import json

from foragent.state import StateStore
from test_one_entrance_seat import enter, hook, invoke, make_pm_case
from test_skills_autosync import isolated_home  # noqa: F401


def test_order_context_exposes_the_persisted_interview_for_the_pm(tmp_path, monkeypatch, capsys, isolated_home):
    case = make_pm_case(tmp_path, monkeypatch)
    assert invoke(capsys, "order", "progress", "start", "--title", "Customer intake").code == 0
    assert invoke(capsys, "order", "progress", "ledger", "--add", "Jin chose Seoul").code == 0
    assert invoke(capsys, "order", "progress", "chunk", "--add", "Export").code == 0
    assert invoke(capsys, "order", "progress", "cell", "--chunk", "Export", "--slot", "goal",
                  "--ask", "Which export format?").code == 0
    result = invoke(capsys, "order", "context", "--json")
    assert result.code == 0, result.text
    progress = json.loads(result.out).get("interview_progress")
    assert progress == StateStore(case.store.root).order_progress(), "FA context must carry the complete persisted interview"
    assert progress is not None


def test_reopening_resumes_ledger_questions_chunks_and_cells(tmp_path, monkeypatch, capsys, isolated_home):
    case = make_pm_case(tmp_path, monkeypatch)
    enter(monkeypatch, capsys, "closed-pm")
    steps = [
        ("start", "--title", "Resume customer intake"),
        ("ledger", "--add", "Jin chose Seoul for customer data"),
        ("chunk", "--add", "Customer intake"),
        ("chunk", "--add", "Export"),
        ("cell", "--chunk", "Customer intake", "--slot", "goal", "--filled"),
        ("cell", "--chunk", "Export", "--slot", "goal", "--ask", "Which export format does Jin want?"),
    ]
    for step in steps:
        result = invoke(capsys, "order", "progress", *step)
        assert result.code == 0, result.text
    saved = StateStore(case.store.root).order_progress()
    monkeypatch.delenv("FORAGENT_SESSION_ID")  # The old shell is gone; only FA's records remain.
    reopened = enter(monkeypatch, capsys, "resumed-pm")
    context = reopened.text + hook(case, monkeypatch, "resumed-pm", "--prompt-context")
    for fact in ("Resume customer intake", "Jin chose Seoul", "Customer intake", "Export", "Which export format"):
        assert fact in context, f"The next PM lacks persisted interview fact: {fact}"
    assert reopened.code == 0
    assert StateStore(case.store.root).order_progress() == saved, "Entering must not reset unfinished cells"
    assert saved["cells"]["Customer intake"]["goal"]["filled"] is True
    assert saved["cells"]["Export"]["goal"]["filled"] is False
    assert saved["cells"]["Export"]["goal"]["ask"] == "Which export format does Jin want?"
    assert "Which export format" in json.dumps(saved)
