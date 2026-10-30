"""Marked journal notes preserve retrospective history and share scheduled controls."""

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path

from foragent import orch_events, orch_inbox, scheduler
from test_architect_project_schedule import review_case  # noqa: F401
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_order_progress import offline_local_cli
from test_orch_review_handoff import tick_context
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


def events(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_retrospectives_append_verbatim_and_status_notes_do_not_replace_history(tmp_path, monkeypatch, capsys):
    case = offline_local_cli(tmp_path, monkeypatch)
    lessons = ["Review slots hid fan-out.\nUse shared capacity.", "보류 중인 새 메모 줄을 보존한다."]
    for lesson in lessons:
        assert cli_exit(case.main, ["orch", "note", "--retrospective", "--now", lesson,
                                   "--why", "Daily review", "--next", "Keep observing",
                                   "--repo-root", str(case.root)]) == 0
        capsys.readouterr()
        assert cli_exit(case.main, ["orch", "note", "--now", "Changed the queue", "--repo-root", str(case.root)]) == 0
        capsys.readouterr()
    rows = events(case.store)
    notes = [r for r in rows if r["kind"] == "note"]
    assert len(notes) == 4
    marked = [r for r in notes if r.get("retrospective") is True]
    assert [r["now"] for r in marked] == lessons
    assert all(r["why"] == "Daily review" and r["next"] == "Keep observing" and r["where"] for r in marked)
    assert orch_inbox.read_note(case.store)["now"] == "Changed the queue"
    assert cli_exit(case.main, ["orch", "note", "--person-found", "Person noticed the missing record",
                                "--repo-root", str(case.root)]) == 0
    assert len([r for r in events(case.store) if r["kind"] == "person-found"]) == 1
    before = (case.store.root / "orch-events.jsonl").read_bytes()
    assert cli_exit(case.main, ["orch", "note", "--retrospective", "--now", " ", "--repo-root", str(case.root)]) != 0
    assert (case.store.root / "orch-events.jsonl").read_bytes() == before


def test_daily_retrospective_wake_is_derived_from_record_and_not_status_note(review_case):
    case = review_case
    case.ctx.config = replace(case.ctx.config, roles={"orchestrator": {
        "agent": "fa-orchestrator", "skills": ["fa-orchestrator"],
        "memory": ".claude/agent-memory/fa-orchestrator/", "signals": []}})
    at = case.clock[0]
    orch_events.append_event(case.store, at, None, "note", "first review", now="first review", retrospective=True)
    case.clock[0] += timedelta(hours=23, minutes=59)
    scheduler.tick(tick_context(case))
    assert not [r for r in orch_inbox.requests(case.store) if r.get("kind") == "retrospective"]
    orch_inbox.write_note(case.store, "Watching the lanes", None, None, {"kind": "terminal"})
    case.clock[0] += timedelta(minutes=1)
    scheduler.tick(tick_context(case))
    due = [r for r in orch_inbox.requests(case.store) if r.get("kind") == "retrospective"]
    assert len(due) == 1, "Daily wake uses marked journal note, not latest status note"
    scheduler.tick(tick_context(case))
    assert [r for r in orch_inbox.requests(case.store) if r.get("kind") == "retrospective"] == due
    orch_events.append_event(case.store, case.clock[0], None, "note", "second review", now="second review", retrospective=True)
    orch_inbox.set_request_status(case.store, due[0]["id"], "done", "Recorded lessons")
    case.clock[0] += timedelta(hours=24)
    scheduler.tick(tick_context(case))
    assert len([r for r in orch_inbox.requests(case.store) if r.get("kind") == "retrospective"]) == 2
    # Retention must retain the latest completion as the source of last-retro.
    case.clock[0] += timedelta(days=40)
    case.store.prune_logs(case.clock[0])
    assert any(r["kind"] == "note" and r.get("retrospective") is True and r.get("now") == "second review"
               for r in events(case.store))


def test_retrospective_skill_uses_record_and_orchestrator_memory_within_line_cap():
    root = Path(__file__).resolve().parents[2]
    skill = (root / "skills/fa-orchestrator/SKILL.md").read_text()
    detail = (root / "skills/fa-orchestrator/references/retrospective.md").read_text()
    assert len(skill.splitlines()) <= 150
    assert "fa orch note" in skill + detail and "--retrospective" in skill + detail
    assert "MEMORY.md" in detail and "append" in detail.lower()
    assert "retrospective" in detail.lower() and "24" in detail
    assert "references/retrospective.md" in skill
