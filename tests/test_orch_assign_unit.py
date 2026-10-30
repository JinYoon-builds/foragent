"""Boundaries of D80 issue assignments and the goal beyond the acceptance test."""

from datetime import UTC, datetime, timedelta

import pytest

from foragent import orch_assign, orch_plan
from foragent.config import Config
from foragent.orch_plan import PlanRefused
from foragent.scheduler_quota import decide
from foragent.state import StateStore

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
KNOWN = {"claude", "codex"}


def store_at(tmp_path, clock=None):
    clock = clock or [NOW]
    return StateStore(tmp_path / "state", now=lambda: clock[0])


def test_assignment_picks_a_non_default_executor_and_model_with_a_cross_reviewer(tmp_path):
    store = store_at(tmp_path)
    config = Config(executor="codex")
    plain = decide(store, config, "main", NOW, 4)
    assert plain.config.executor == "codex" and plain.model is None
    orch_assign.assign(store, 4, "claude", "chosen-model", None, "why", "human", KNOWN)
    start = decide(store, config, "main", NOW, 4)
    assert not start.held
    assert (start.config.executor, start.config.executor_model, start.model) == (
        "claude", "chosen-model", "chosen-model")
    assert start.config.reviewer != "claude" and start.switch is None
    # Another issue keeps ordinary selection.
    assert decide(store, config, "main", NOW, 5).config.executor == "codex"


def test_assignment_never_bypasses_a_hard_limit(tmp_path):
    store = store_at(tmp_path)
    orch_assign.assign(store, 4, "claude", "chosen-model", None, "why", "human", KNOWN)
    store.pause_executor("claude", NOW + timedelta(hours=1))
    start = decide(store, Config(executor="codex"), "main", NOW, 4)
    assert start.held, "A limited assigned executor waits; it never runs another executor"


def test_assignment_lapses_at_until_and_lapsed_items_leave_the_plan(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    orch_assign.assign(store, 4, "claude", "m", NOW + timedelta(minutes=10), "why", "human", KNOWN)
    assert orch_assign.assignment(store, 4, NOW + timedelta(minutes=9))["model"] == "m"
    assert orch_assign.assignment(store, 4, NOW + timedelta(minutes=10)) is None
    assert decide(store, Config(executor="codex"), "main", NOW + timedelta(minutes=10), 4).model is None


def test_plan_expiry_keeps_assignments_and_goal(tmp_path):
    clock = [NOW]
    store = store_at(tmp_path, clock)
    orch_plan.set_plan(store, {"main": 2}, NOW + timedelta(minutes=5), "human")
    orch_assign.assign(store, 4, "codex", "m", None, "why", "human", KNOWN)
    orch_assign.set_goal(store, "Ship", NOW + timedelta(days=1), "why", "human")
    later = orch_plan.load(store, NOW + timedelta(minutes=6))
    assert later["width"] == {} and later["assignments"]["4"]["model"] == "m"
    assert later["goal"]["text"] == "Ship"


@pytest.mark.parametrize("call", [
    lambda store: orch_assign.assign(store, 4, "nope", "m", None, "w", "human", KNOWN),
    lambda store: orch_assign.assign(store, 4, "codex", " ", None, "w", "human", KNOWN),
    lambda store: orch_assign.assign(store, 4, "codex", "m", NOW, "w", "human", KNOWN),
    lambda store: orch_assign.set_goal(store, "x", NOW - timedelta(seconds=1), "w", "human"),
    lambda store: orch_assign.set_goal(store, " ", NOW + timedelta(days=1), "w", "human"),
])
def test_invalid_edits_change_nothing_and_journal_nothing(tmp_path, call):
    store = store_at(tmp_path)
    with pytest.raises(PlanRefused):
        call(store)
    assert not (store.root / "plan.json").exists()
    assert not (store.root / "orch-events.jsonl").exists()


def test_orchestrator_may_revise_its_own_choice_and_clearing_nothing_journals_nothing(tmp_path):
    store = store_at(tmp_path)
    orch_assign.assign(store, 4, "codex", "a", None, "w", "orchestrator", KNOWN)
    orch_assign.assign(store, 4, "claude", "b", None, "w", "orchestrator", KNOWN)
    assert orch_assign.clear_assignment(store, 4, "w", "orchestrator") is True
    events = (store.root / "orch-events.jsonl").read_text().splitlines()
    assert orch_assign.clear_assignment(store, 4, "w", "human") is False
    assert orch_assign.clear_goal(store, "w", "human") is False
    assert (store.root / "orch-events.jsonl").read_text().splitlines() == events
    assert len(events) == 3


def test_configured_executor_without_an_adapter_is_refused_and_keeps_the_prior_choice(tmp_path):
    store = store_at(tmp_path)
    config = Config(executor="codex", executor_list=[{"name": "codex", "quota": "subscription"},
                                                 {"name": "missing-adapter", "quota": "unlimited"}])
    known = orch_assign.known_executors(config)
    assert known == {"claude", "codex"}
    orch_assign.assign(store, 4, "codex", "valid", None, "w", "human", known)
    with pytest.raises(PlanRefused, match="missing-adapter"):
        orch_assign.assign(store, 4, "missing-adapter", "m", None, "w", "human", known)
    assert orch_assign.assignment(store, 4)["model"] == "valid"


def test_stored_assignment_without_an_adapter_never_reaches_admission(tmp_path):
    """A config change can leave a stored assignment unrunnable; ordinary selection applies."""
    store = store_at(tmp_path)
    orch_assign.assign(store, 4, "missing-adapter", "m", None, "w", "human", {"missing-adapter"})
    start = decide(store, Config(executor="codex"), "main", NOW, 4)
    assert start.config.executor == "codex" and start.model is None and not start.held


def test_assigned_start_config_carries_no_fallback_model(tmp_path):
    store = store_at(tmp_path)
    orch_assign.assign(store, 4, "codex", "assigned", None, "w", "human", KNOWN)
    start = decide(store, Config(executor="codex", executor_model="lane", executor_fallback_model="fb"),
                   "main", NOW, 4)
    assert (start.config.executor_model, start.config.executor_fallback_model) == ("assigned", None)
