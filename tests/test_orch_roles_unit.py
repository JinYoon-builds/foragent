"""Edges of role signals, facts kinds and role decisions the role-signals acceptance test does not pin."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch_facts_kinds, orch_inbox, orch_roles, orch_scorecard
from foragent.orch_facts_resources import _jsonl
from foragent.state import StateStore

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.clock = clock
    return store


def finish(store, number, *, at, outcome):
    store.clock[0] = at - timedelta(minutes=5)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    store.clock[0] = at
    store.finish_run(number, outcome=outcome)
    store.clock[0] = NOW


@pytest.mark.parametrize("finishes,expected", (
    ([(1, 1), (1, 2)], True),
    ([(1, 1)], False),
    ([(1, 1), (2, 2)], False),
    ([(1, 1), (1, 24 * 8)], False),
), ids=("same-issue-twice", "once", "two-issues", "one-outside-7-days"))
def test_repeated_design_holds_counts_held_executions_of_one_issue_in_7_days(store, finishes, expected):
    for number, hours in finishes:
        finish(store, number, at=NOW - timedelta(hours=hours), outcome="held")
    finish(store, 3, at=NOW - timedelta(hours=1), outcome="merged")
    assert orch_facts_kinds.repeated_design_holds(store, NOW) is expected


def row(issue, hours, files):
    return {"issue": issue, "started_at": (NOW - timedelta(hours=hours)).isoformat(),
            "overlap_records": [{"file": path} for path in files]}


def test_hot_overlap_files_needs_three_distinct_executions_started_in_24_hours():
    rows = [row(1, 1, ["a.py"]), row(2, 2, ["a.py", "a.py"]), row(3, 3, ["b.py"])]
    assert orch_facts_kinds.hot_overlap_files(rows, NOW) is False
    assert orch_facts_kinds.hot_overlap_files([*rows, row(4, 23, ["a.py"])], NOW) is True
    assert orch_facts_kinds.hot_overlap_files([*rows, row(4, 25, ["a.py"])], NOW) is False


def test_files_over_limit_reads_src_python_files_above_the_limit(tmp_path):
    module = tmp_path / "src/pkg/module.py"
    module.parent.mkdir(parents=True)
    module.write_text("x = 1\n" * orch_facts_kinds.LINE_LIMIT)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_big.py").write_text("x = 1\n" * 900)
    assert orch_facts_kinds.files_over_limit(tmp_path) is False
    module.write_text("x = 1\n" * (orch_facts_kinds.LINE_LIMIT + 1))
    assert orch_facts_kinds.files_over_limit(tmp_path) is True


def waiting(lane, count, labels=("approved", "ready")):
    return [SimpleNamespace(lane=lane, labels=set(labels)) for _ in range(count)]


def test_idle_lanes_with_backlog_needs_an_idle_other_lane_and_three_ready_issues():
    config = SimpleNamespace(lanes={"orch": {}, "view": {}})
    busy = [SimpleNamespace(lane="view"), SimpleNamespace(lane="main")]
    assert orch_facts_kinds.idle_lanes_with_backlog(config, busy, waiting("orch", 3)) is False
    assert orch_facts_kinds.idle_lanes_with_backlog(config, busy, waiting("view", 3)) is True
    assert orch_facts_kinds.idle_lanes_with_backlog(config, busy, waiting("view", 2)) is False
    assert orch_facts_kinds.idle_lanes_with_backlog(config, busy, waiting("view", 3, ("approved",))) is False


def test_role_decision_appends_one_event_and_closes_the_request(store):
    named = orch_inbox.add_request(store, "gate", "security signal met", {"role": "security"})
    bare = orch_inbox.add_request(store, "planning", "proposal")
    event = orch_roles.decide(store, named["id"], "accepted", "docs")
    assert event["role"] == "security", "The request's own role wins over --role"
    with pytest.raises(orch_inbox.InboxError):
        orch_roles.decide(store, bare["id"], "rejected", None)
    orch_roles.decide(store, bare["id"], "rejected", "docs")
    decisions = [row for row in _jsonl(store.root / "orch-events.jsonl") if row["kind"] == "role-proposal"]
    assert [(row["role"], row["request_id"], row["accepted"]) for row in decisions] == [
        ("security", named["id"], True), ("docs", bare["id"], False)]
    assert {row["id"]: row["status"] for row in orch_inbox.requests(store)} == {named["id"]: "done", bare["id"]: "done"}


def test_scorecard_previous_window_holds_the_median_per_role_day(store):
    from foragent.orch_events import append_event

    for days in (1, 1, 2, 2, 3):
        append_event(store, NOW - timedelta(days=days, hours=1), None, "docs-call", "call")
    append_event(store, NOW - timedelta(hours=1), None, "role-proposal", "decision", role="docs", accepted=True)
    append_event(store, NOW - timedelta(hours=2), None, "role-proposal", "decision", role="docs", accepted=False)
    config = SimpleNamespace(roles={"docs": {"agent": "fa-docs"}})
    card = orch_roles.scorecard(store, config, orch_scorecard.collect(store, days=4))
    no_signals = {"signals": {"raised": 0, "called": 0, "not_now": 0, "unanswered": 0}}
    assert card["window"]["today"]["roles"] == {"docs": {"calls": 0, "accepted_proposals": 1, **no_signals,
                                                             "open_signals": 0}}
    assert card["window"]["previous"]["roles"] == {"docs": {"calls": 2, "accepted_proposals": 0, **no_signals}}
    assert orch_scorecard.human(card, "en").count("Role docs: calls 0 (before 2)") == 1


def test_repeating_a_role_decision_counts_once_and_the_latest_one_wins(store, tmp_path):
    config = SimpleNamespace(roles={"security": {"agent": "fa-security", "created_at": NOW.isoformat()}})
    requests = [orch_inbox.add_request(store, "gate", f"proposal {n}", {"role": "security"}) for n in range(3)]
    for step in range(3):
        store.clock[0] = NOW - timedelta(minutes=10 - step)
        orch_roles.decide(store, requests[0]["id"], "rejected", None)
    store.clock[0] = NOW
    rows = orch_roles.collect(store, config, tmp_path)["roles"]
    assert rows["security"]["removal_proposed"] is False, "One proposal rejected three times is one rejection"
    for request in requests[1:]:
        orch_roles.decide(store, request["id"], "rejected", None)
    assert orch_roles.collect(store, config, tmp_path)["roles"]["security"]["removal_proposed"] is True
    store.clock[0] = NOW + timedelta(minutes=1)
    orch_roles.decide(store, requests[0]["id"], "accepted", None)
    orch_roles.decide(store, requests[0]["id"], "accepted", None)
    assert orch_roles.collect(store, config, tmp_path)["roles"]["security"]["removal_proposed"] is False, (
        "A corrected decision replaces the earlier one")
    card = orch_roles.scorecard(store, config, orch_scorecard.collect(store, days=2))
    assert card["window"]["today"]["roles"]["security"]["accepted_proposals"] == 1
