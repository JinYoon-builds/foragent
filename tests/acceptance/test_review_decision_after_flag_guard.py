"""Regression controls for review-decision-after-flag (not in Done when; these pass on main).

A release must come from a releasing answer to the hold's request: an unanswered request,
or a non-releasing note, keeps `fa orch retry` refused even when the gate flags again; an
answer after the flag keeps releasing.
"""

from datetime import timedelta

from foragent import orch_events, orch_inbox
from foragent.reasons import Reason
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_decision_after_flag import (
    answer, assert_retry_queued, bound_cli, gate_flags_again)
from test_review_decision_hold_reader import pending_review_decision
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


def assert_refused(case, main, capsys):
    capsys.readouterr()
    retries = len(orch_events.events(case.store, "retry"))
    code = main(["orch", "retry", "1"])
    capsys.readouterr()
    assert code not in (0, None), "The hold must still refuse the retry"
    assert "ready" not in case.tracker.get_issue(1).labels
    assert len(orch_events.events(case.store, "retry")) == retries
    assert case.store.load_flag(1).reason == Reason.REVIEW_DEFECTS


def test_unanswered_request_keeps_refusing_after_a_late_flag(git_history, tmp_path, monkeypatch, capsys):
    case, _script, _request = pending_review_decision(git_history, tmp_path, monkeypatch)
    main = bound_cli(case, monkeypatch)
    case.clock[0] += timedelta(seconds=5)
    gate_flags_again(case)
    case.clock[0] += timedelta(minutes=1)
    assert_refused(case, main, capsys)


def test_non_releasing_note_keeps_refusing_after_a_late_flag(git_history, tmp_path, monkeypatch, capsys):
    case, _script, request = pending_review_decision(git_history, tmp_path, monkeypatch)
    main = bound_cli(case, monkeypatch)
    case.clock[0] += timedelta(minutes=1)
    orch_inbox.set_request_status(case.store, request["id"], "done",
                                  answer="escalated to the PM", require_answer=True)
    case.clock[0] += timedelta(seconds=5)
    gate_flags_again(case)
    case.clock[0] += timedelta(minutes=1)
    assert_refused(case, main, capsys)


def test_answer_after_the_flag_still_releases(git_history, tmp_path, monkeypatch, capsys, kicks):
    case, _script, request = pending_review_decision(git_history, tmp_path, monkeypatch)
    main = bound_cli(case, monkeypatch)
    case.clock[0] += timedelta(minutes=1)
    answer(case, request)
    case.clock[0] += timedelta(minutes=1)
    assert_retry_queued(case, main, capsys, kicks)
