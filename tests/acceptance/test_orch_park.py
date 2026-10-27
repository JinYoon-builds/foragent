"""Acceptance test for orch-park. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import timedelta
import json

from foragent.scheduler import tick
from test_blocker_timeout_clock import escalations
from test_board_status_groups import offline_github  # noqa: F401
from test_orch_review_handoff import tick_context
from test_orch_verbs import blocked_case
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


def park_case(git_history, tmp_path, monkeypatch):
    case, main = blocked_case(git_history, tmp_path, monkeypatch)
    case.ctx.config = replace(case.config, orchestrator_timeout_minutes=5, notify="none")
    return case, main


def orch_events(case):
    path = case.store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_park_clears_blocker_records_reason_and_prevents_timeout(
    git_history, tmp_path, monkeypatch,
):
    case, main = park_case(git_history, tmp_path, monkeypatch)
    case.tracker.add_labels(1, ["needs-human"])
    before = orch_events(case)
    why = "Wait for the upstream locale decision"

    assert cli_exit(main, ["orch", "park", "1", "--why", why]) == 0, "fa orch park must succeed"
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    added = orch_events(case)[len(before):]
    assert len(added) == 1, "Parking must append exactly one orchestrator event"
    assert added[0]["kind"] == "parked"
    assert added[0]["issue"] == 1
    assert why in added[0]["summary"], "The parked event must preserve the reason"

    case.clock[0] += timedelta(minutes=case.ctx.config.orchestrator_timeout_minutes + 1)
    assert tick(tick_context(case, start_runs=False)).skipped is None
    assert "needs-human" not in case.tracker.get_issue(1).labels
    assert escalations(case) == [], "A parked blocker must not generate an orchestrator timeout"


def test_park_for_person_and_unpark_update_label_and_journal(
    git_history, tmp_path, monkeypatch,
):
    case, main = park_case(git_history, tmp_path, monkeypatch)
    before = orch_events(case)
    why = "The person will choose the locale"

    assert cli_exit(main, ["orch", "park", "1", "--why", why, "--for-person"]) == 0, (
        "fa orch park must accept --for-person"
    )
    assert "waiting-person" in case.tracker.get_issue(1).labels
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    parked = orch_events(case)[len(before):]
    assert len(parked) == 1 and parked[0]["kind"] == "parked"
    assert parked[0]["issue"] == 1 and why in parked[0]["summary"]

    before = orch_events(case)
    assert cli_exit(main, ["orch", "unpark", "1"]) == 0, "fa orch unpark must succeed"
    assert "waiting-person" not in case.tracker.get_issue(1).labels
    added = orch_events(case)[len(before):]
    assert len(added) == 1, "Unparking must append exactly one orchestrator event"
    assert added[0]["kind"] == "unparked" and added[0]["issue"] == 1


def test_park_requires_why_without_changing_labels_or_events(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = park_case(git_history, tmp_path, monkeypatch)
    case.tracker.add_labels(1, ["needs-human"])
    before_issue, before_events = case.tracker.get_issue(1), orch_events(case)
    capsys.readouterr()

    assert cli_exit(main, ["orch", "park", "1"]) != 0
    error = capsys.readouterr().err
    assert case.tracker.get_issue(1) == before_issue, "Missing --why must leave the issue unchanged"
    assert orch_events(case) == before_events, "Missing --why must not append an event"
    # An unknown subcommand must not satisfy required-option validation.
    assert "--why" in error, "fa orch park must report the missing --why option"


def test_unparked_blocker_still_escalates_once_after_timeout(
    git_history, tmp_path, monkeypatch,
):
    case, main = park_case(git_history, tmp_path, monkeypatch)
    context = tick_context(case, start_runs=False)
    case.clock[0] += timedelta(minutes=case.ctx.config.orchestrator_timeout_minutes - 1)
    assert tick(context).skipped is None
    assert "needs-human" not in case.tracker.get_issue(1).labels
    assert escalations(case) == []

    case.clock[0] += timedelta(minutes=2)
    assert tick(context).skipped is None
    assert "needs-human" in case.tracker.get_issue(1).labels
    assert len(escalations(case)) == 1, "An unparked blocker must still escalate after its timeout"
    assert tick(context).skipped is None
    assert len(escalations(case)) == 1, "Repeated ticks must not duplicate the timeout comment"
