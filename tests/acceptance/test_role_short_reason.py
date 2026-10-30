"""Acceptance test for role-short-reason. Locked by Foragent; do not edit."""

import json

import pytest

from test_orch_architect import WHY, architect_case, finish_session
from test_orch_inbox import inbox_command
from test_role_source import DEFAULTS, role_entry, write_role_snapshot
from test_skills_autosync import isolated_home


@pytest.mark.parametrize("command,role", [("architect", "architect"), ("role", "architect"), ("role", "docs")])
@pytest.mark.parametrize("language,short", [("ko", "레인 경계를 살펴봐요"), ("en", "checking lane boundaries")])
def test_caller_supplies_short_reason_verbatim_beside_full_why(tmp_path, monkeypatch, capsys, command, role, language, short):
    case = architect_case(tmp_path, monkeypatch)
    config = case.root / ".foragent.yml"
    config.write_text(config.read_text(encoding="utf-8").replace("language: en", f"language: {language}"), encoding="utf-8")
    write_role_snapshot(case.root, {name: role_entry(name) for name in DEFAULTS})
    args = [command, *([role] if command == "role" else []), "--why", WHY, "--short", short]
    capsys.readouterr()
    code = inbox_command(case, *args)
    output = capsys.readouterr()
    assert code == 0, f"r55(b) needs caller-authored --short for both role entrypoints: {output.err.strip()}"
    assert len(case.launches) == 1 and case.turns == []
    record = json.loads((case.store.root / f"{role}.json").read_text(encoding="utf-8"))
    assert record["why"] == WHY and record.get("short") == short, "persist both reasons without interpreting or truncating either"
    events = [json.loads(line) for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()]
    event, = [row for row in events if row["kind"] == f"{role}-call"]
    assert event.get("why") == WHY and event.get("short") == short, "the call event keeps the same paired evidence"
    finish_session(case, case.launches[0])
    assert WHY in case.turns[0]["prompt"], "the role still receives the full evidence"
    assert case.gh_calls == [] and case.pushes == []


def test_old_callers_without_short_still_work_and_do_not_fabricate_a_summary(tmp_path, monkeypatch):
    case = architect_case(tmp_path, monkeypatch)
    assert inbox_command(case, "architect", "--why", WHY) == 0
    record = json.loads((case.store.root / "architect.json").read_text())
    assert record["why"] == WHY
    assert record.get("short") in (None, ""), "legacy absence stays absent; the script must not decide how to summarise why"
