"""Acceptance test for orch-say. Locked by Foragent; do not edit."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from test_skills_autosync import isolated_home


def speaking_case(git_history, tmp_path, monkeypatch):
    from test_orch_merge import premerge_case

    case, main, pr, _ = premerge_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.ctx.config, language="ko", review="fast", skill_libraries=[])
    case.store.finish_run(1, outcome="blocked")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["blocked"])
    case.tracker.comment(1, "<!-- foragent:blocked reason=review-dispute -->\nLocale mismatch")
    monkeypatch.setattr("foragent.cli.RunContext", lambda **kw: case.ctx)
    return case, main


def event_frame(case, main, capsys):
    from foragent.watch import collect, render_frame

    capsys.readouterr()
    assert main(["gate", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    snapshot = collect(status, case.tracker, case.store, case.ctx.config, case.clock[0])
    return render_frame(snapshot, case.ctx.config, case.root, width=160)


@pytest.mark.parametrize("verb,options", [
    ("retry", []), ("rerun-review", []),
    ("resolve", ["--verdict", "fail", "--note", "Fix the locale"]),
    ("escalate", ["--summary", "Choose locale", "--tried", "Reviewed", "--option", "Korean"]),
])
def test_orch_say_commands_keep_raw_summary_and_render_person_line(
    git_history, tmp_path, monkeypatch, capsys, verb, options,
):
    case, main = speaking_case(git_history, tmp_path, monkeypatch)
    say = "#1의 언어 처리를 다시 살펴봐요"
    assert main(["orch", verb, "1", *options, "--say", say]) == 0
    events = [json.loads(line) for line in (case.store.root / "orch-events.jsonl").read_text().splitlines()]
    event = next(event for event in reversed(events) if event["kind"] == verb)
    assert event["say"] == say
    assert event["summary"] and event["summary"] != say
    frame = event_frame(case, main, capsys)
    assert say in frame
    assert event["summary"] not in frame
    assert "queued implementation retry" not in frame


def test_orch_say_missing_say_renders_localized_kind_and_issue(git_history, tmp_path, monkeypatch, capsys):
    case, main = speaking_case(git_history, tmp_path, monkeypatch)
    assert main(["orch", "retry", "1"]) == 0
    frame = event_frame(case, main, capsys)
    assert any("#1" in line and "다시 맡겼어요" in line for line in frame.splitlines())
    assert "queued implementation retry" not in frame


def test_orch_say_skill_documents_person_line_and_voice():
    path = Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "--say" in text
    assert "docs/voice.md" in text
