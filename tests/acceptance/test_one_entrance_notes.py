"""Reuse #1043's D85 public note queue; scope ends at keeping the note in FA."""

import json

from test_one_entrance_seat import enter, invoke, make_pm_case
from test_skills_autosync import isolated_home  # noqa: F401


def test_decision_notes_survive_the_pm_without_editing_the_decision_document(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    docs = case.root / "docs"
    docs.mkdir()
    document = docs / "decisions.md"
    document.write_text("# Existing decisions\n")
    enter(monkeypatch, capsys, "notes-pm")
    decisions = ["Jin chose PM as the entrance name.", "Jin left note-to-document promotion open."]
    for index, text in enumerate(decisions):
        if index:
            enter(monkeypatch, capsys, "replacement-pm")
            monkeypatch.setenv("FORAGENT_SESSION_ID", "notes-pm")
        result = invoke(capsys, "order", "note", "--defer", "next-code-merge", "--kind", "decision", "--text", text)
        assert result.code == 0, "PM decision notes must use the durable D85 append route: " + result.text
    monkeypatch.delenv("FORAGENT_SESSION_ID")
    enter(monkeypatch, capsys, "next-notes-pm")
    for _ in range(2):
        result = invoke(capsys, "order", "pending", "--json")
        assert result.code == 0, result.text
        rows = json.loads(result.out)["notes"]
        for text in decisions:
            assert sum(row["kind"] == "decision" and row["text"] == text for row in rows) == 1
    assert document.read_text() == "# Existing decisions\n"
    assert case.events == [] and case.tracker.list_issues() == []
