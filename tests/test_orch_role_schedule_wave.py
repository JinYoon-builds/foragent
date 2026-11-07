"""Boundaries of the UX researcher's order wave: which done events cover which orders."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from foragent import orch_role_schedule as schedule

ROLE = "ux-researcher"
NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)


@pytest.fixture
def due(monkeypatch):
    entry = {"signals": [{"kinds": ["order_finished"]}]}
    monkeypatch.setattr(schedule, "_entry", lambda config, role: entry)
    monkeypatch.setattr(schedule, "suppression", lambda store, config, now: None)
    monkeypatch.setattr(schedule, "_session_is_live", lambda store, role: False)
    monkeypatch.setattr(schedule, "_next_retry", lambda store, config, failure, now: None)
    config = SimpleNamespace(roles=[ROLE], language="en")
    return lambda events: schedule._next_pass(None, config, ROLE, NOW, events)


def finished(order, minute):
    at = f"2026-11-06T05:{minute:02d}:00+00:00"
    return {"at": at, "kind": schedule.ORDER_FINISHED, "order": order, "finished_at": at}


def done(scope, exit_code=0):
    return {"at": "2026-11-06T06:00:00+00:00", "kind": f"{ROLE}-done", "exit_code": exit_code,
            "started_at": "2026-11-06T06:00:00+00:00", "scope": scope}


def test_every_uncovered_order_is_one_wave_in_finish_order(due):
    events = [finished(902, 3), finished(900, 1), finished(901, 2)]
    assert due(events) == {"pass": "order", "orders": [900, 901, 902]}


def test_a_legacy_single_order_scope_still_covers_its_order(due):
    events = [finished(900, 1), finished(901, 2), done({"pass": "order", "order": 900})]
    assert due(events) == {"pass": "order", "orders": [901]}


def test_a_successful_wave_covers_all_its_orders(due):
    events = [finished(900, 1), finished(901, 2), done({"pass": "order", "orders": [900, 901]})]
    assert due(events) is None


def test_a_failed_session_covers_nothing(due):
    events = [finished(900, 1), finished(901, 2), done({"pass": "order", "orders": [900, 901]}, exit_code=1)]
    assert due(events) == {"pass": "order", "orders": [900, 901]}


def test_the_prompt_names_every_order_of_the_wave():
    config = SimpleNamespace(language="en")
    why = schedule._pass_why(config, ROLE, {"pass": "order", "orders": [900, 901]}, [])
    assert "#900, #901" in why
