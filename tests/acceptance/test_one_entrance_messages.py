"""The durable question box is the orchestrator's only person-message route."""

import json

from foragent import cli
from test_one_entrance_seat import invoke, make_pm_case
from test_skills_autosync import isolated_home  # noqa: F401


def test_question_waits_in_fa_when_no_pm_exists_without_direct_delivery(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    pushes = []
    monkeypatch.setattr(cli.notify, "send", lambda *args, **kwargs: pushes.append((args, kwargs)))
    monkeypatch.setenv("FORAGENT_ROLE", "orchestrator")
    result = invoke(capsys, "orch", "ask", "--question", "Which customer region?", "--option", "Seoul",
                    "--option", "Tokyo", "--recommend", "1", "--json")
    assert result.code == 0, result.text
    question = json.loads(result.out)
    listed = invoke(capsys, "orch", "questions", "--json")
    rows = json.loads(listed.out)
    row, = [r for r in rows if r["id"] == question["id"]]
    assert row["question"] == "Which customer region?" and row["options"] == ["Seoul", "Tokyo"]
    assert row["recommend"] == 1
    assert pushes == [], "A hidden orchestrator must leave person messages for the PM, not push them directly"
