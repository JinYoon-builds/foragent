"""The active PM receives questions, records and project lessons through its context."""

import json

from foragent import cli
from test_one_entrance_seat import enter, hook, invoke, make_pm_case
from test_skills_autosync import isolated_home  # noqa: F401


def test_orchestrator_question_reaches_only_the_current_pm_and_survives_reopening(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    pushes = []
    monkeypatch.setattr(cli.notify, "send", lambda *args, **kwargs: pushes.append((args, kwargs)))
    enter(monkeypatch, capsys, "old-relay-pm")
    enter(monkeypatch, capsys, "current-relay-pm")
    monkeypatch.setenv("FORAGENT_ROLE", "orchestrator")
    result = invoke(capsys, "orch", "ask", "--question", "Which storage region does Jin want?",
                    "--option", "Seoul", "--option", "Tokyo", "--recommend", "1", "--json")
    assert result.code == 0, result.text
    question = json.loads(result.out)
    monkeypatch.delenv("FORAGENT_ROLE")
    context = hook(case, monkeypatch, "current-relay-pm", "--prompt-context")
    for fact in (question["id"], "Which storage region", "Seoul", "Tokyo"):
        assert fact in context, f"The PM lacks the orchestrator's question context: {fact}"
    assert "Which storage region" not in hook(case, monkeypatch, "old-relay-pm", "--prompt-context")
    assert pushes == [], "Orchestrator questions go through the PM, never directly to the person"
    monkeypatch.delenv("FORAGENT_SESSION_ID", raising=False)
    resumed = enter(monkeypatch, capsys, "new-relay-pm")
    assert "Which storage region" in resumed.text + hook(case, monkeypatch, "new-relay-pm", "--prompt-context")
    assert invoke(capsys, "orch", "answer", question["id"], "--text", "Seoul").code == 0
    assert "Which storage region" not in hook(case, monkeypatch, "new-relay-pm", "--prompt-context")


def test_project_pm_notebook_reaches_the_next_teammate_and_retains_pending_lessons(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    folder = case.root / ".claude/agent-memory/fa-pm"
    folder.mkdir(parents=True)
    curated = "# PM lessons\nCheck labels after approval.\n"
    pending = "- [id:issue-81-choice] #81 PM: Questions offer multiple choices.\n"
    (folder / "MEMORY.md").write_text(curated)
    (folder / "MEMORY.new.md").write_text(pending)
    enter(monkeypatch, capsys, "teammate-pm")
    context = hook(case, monkeypatch, "teammate-pm", "--prompt-context")
    assert "Check labels after approval" in context, "The next PM must inherit the repo's PM lessons"
    assert context.count("Questions offer multiple choices") == 1
    assert (folder / "MEMORY.md").read_text() == curated
    assert (folder / "MEMORY.new.md").read_text() == pending, "Entering is a reader, not a second notebook curator"


def test_pm_inspection_context_includes_the_recorded_reason_and_next_step(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    assert invoke(capsys, "orch", "note", "--now", "Reviewing issue 41", "--why", "The locale contract changed",
                  "--next", "Ask the owner about the region").code == 0
    enter(monkeypatch, capsys, "inspection-pm")
    context = hook(case, monkeypatch, "inspection-pm", "--prompt-context")
    for fact in ("Reviewing issue 41", "The locale contract changed", "Ask the owner about the region"):
        assert fact in context, "The PM needs the actual records to explain the orchestrator: " + fact


def test_lane_screen_shows_one_line_of_the_orchestrators_current_work(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    assert invoke(capsys, "orch", "note", "--now", "Reviewing issue 41", "--why", "An internal diagnostic detail",
                  "--next", "An internal next action").code == 0
    result = invoke(capsys, "gate", "watch", "--once")
    assert result.code == 0, result.text
    assert result.out.count("Reviewing issue 41") == 1
    assert "An internal diagnostic detail" not in result.out and "An internal next action" not in result.out
