"""hot_overlap_files is met only by lost work on a file in 24 hours (#2571)."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from foragent import orch_events, run_overlap
from foragent.orch_facts_kinds import hot_overlap_files, hot_overlap_files_numbers
from foragent.state import StateStore

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))
from test_optimistic_overlap import STATE as OVERLAP_STATE, _overlap, _recorded, offline_overlap  # noqa: E402,F401
from test_orch_facts import facts, facts_case  # noqa: E402
from test_skills_autosync import isolated_home  # noqa: E402,F401
from test_state_split import offline_acceptance  # noqa: E402,F401

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
STATE = "src/foragent/state.py"
CLI = "src/foragent/cli.py"


def started(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def row(issue, *, hours, records=(), outcome="merged"):
    return {"issue": issue, "started_at": started(hours), "outcome": outcome, "overlap_records": list(records)}


def record(path, *, redo=0, seconds=0, conflicted=False):
    return {"file": path, "issues": [1], "conflicted": conflicted, "resolution_seconds": seconds, "redo_count": redo}


def test_co_presence_and_resolved_conflicts_are_no_lost_work():
    rows = [row(n, hours=n, records=[record(STATE, conflicted=n == 3, seconds=40 * (n == 3))]) for n in (1, 2, 3, 4)]
    assert hot_overlap_files_numbers(rows, NOW) == {}
    assert not hot_overlap_files(rows, NOW)


def test_a_retries_exceeded_run_without_a_budget_stop_record_is_no_lost_work():
    """Review round 1: a transient executor failure during conflict resolution also ends on
    retries-exceeded with conflicts left; only a recorded conflict-budget stop counts."""
    rows = [row(10, hours=2, outcome="retries-exceeded", records=[record(STATE, conflicted=True, seconds=55)])]
    assert hot_overlap_files_numbers(rows, NOW, {}) == {}
    # A stop recorded for another execution of the same issue names nothing here.
    assert hot_overlap_files_numbers(rows, NOW, {(10, started(30)): {STATE}}) == {}


def test_a_conflict_budget_stop_names_the_file_with_its_stop_count_and_resolution_seconds():
    rows = [row(10, hours=2, outcome="retries-exceeded",
                records=[record(STATE, conflicted=True, seconds=55), record(CLI, conflicted=True, seconds=9)])]
    stops = {(10, started(2)): {STATE}}
    assert hot_overlap_files_numbers(rows, NOW, stops) == {"lost_work_by_file": {
        STATE: {"issues": ["#10"], "rebuilds": 0, "budget_stops": 1, "resolution_seconds": 55}}}


def test_rebuilds_and_stops_add_up_per_file_across_runs_in_the_window_only():
    rows = [
        row(21, hours=1, records=[record(STATE, redo=1, seconds=10)]),
        row(5, hours=5, outcome="retries-exceeded", records=[record(STATE, redo=1, seconds=30)]),
        row(22, hours=25, outcome="retries-exceeded", records=[record(STATE, redo=3, seconds=99)]),
    ]
    stops = {(5, started(5)): {STATE}, (22, started(25)): {STATE}}
    assert hot_overlap_files_numbers(rows, NOW, stops) == {"lost_work_by_file": {
        STATE: {"issues": ["#5", "#21"], "rebuilds": 2, "budget_stops": 1, "resolution_seconds": 40}}}


def test_the_budget_stop_event_round_trips_to_the_numbers(tmp_path):
    store = StateStore(tmp_path / "state")
    store.root.mkdir(parents=True)
    run = SimpleNamespace(started_at=started(1))
    store.get_run = lambda n: run if n == 30 else None
    attempt = SimpleNamespace(n=30, ctx=SimpleNamespace(store=store, now=lambda: NOW))
    run_overlap.record_budget_stop(attempt, [CLI, CLI])
    run_overlap.record_budget_stop(SimpleNamespace(n=31, ctx=attempt.ctx), [STATE])  # no run: nothing
    run_overlap.record_budget_stop(attempt, [])  # no unresolved file: nothing
    assert len(orch_events.events(store, run_overlap.BUDGET_STOP)) == 1
    stops = run_overlap.budget_stops(store)
    assert stops == {(30, started(1)): {CLI}}
    rows = [row(30, hours=1, outcome="retries-exceeded")]
    assert hot_overlap_files_numbers(rows, NOW, stops) == {"lost_work_by_file": {
        CLI: {"issues": ["#30"], "rebuilds": 0, "budget_stops": 1, "resolution_seconds": 0}}}


def test_the_conflict_budget_stop_records_its_unresolved_files(tmp_path, monkeypatch):
    from foragent import run_conflicts
    from foragent.run_attempt import ConflictBudgetExhausted

    store = StateStore(tmp_path / "state")
    store.root.mkdir(parents=True)
    store.get_run = lambda n: SimpleNamespace(started_at=started(1))
    flagged = []
    monkeypatch.setattr(run_conflicts, "flag", lambda *args: flagged.append(args))
    monkeypatch.setattr(run_conflicts, "_append", lambda *args: None)
    attempt = SimpleNamespace(n=40, log=tmp_path / "log", ctx=SimpleNamespace(
        store=store, now=lambda: NOW, config=SimpleNamespace(language="en", conflict_retries=2)))
    stopped = run_conflicts._stop_conflicts(attempt, ConflictBudgetExhausted("failed", [STATE], [STATE, CLI]))
    assert stopped.outcome == "retries-exceeded" and len(flagged) == 1
    assert run_overlap.budget_stops(store) == {(40, started(1)): {STATE}}


def test_fa_orch_facts_counts_a_recorded_budget_stop_and_not_a_bare_retries_exceeded(tmp_path, monkeypatch, capsys):
    case = facts_case(tmp_path, monkeypatch)
    for number in (70, 71):
        _recorded(case, number, hours=2, declared=[OVERLAP_STATE], changed=[OVERLAP_STATE],
                  outcome="retries-exceeded", overlaps=[_overlap(OVERLAP_STATE, conflict=True, seconds=21)])
    assert "hot_overlap_files" not in facts(case, capsys)["kinds"], "retries-exceeded alone is no budget stop"
    saved = json.loads((case.store.root / "runs" / "71.json").read_text())
    orch_events.append_event(case.store, NOW, 71, run_overlap.BUDGET_STOP, "stop",
                             started_at=saved["started_at"], files=[OVERLAP_STATE])
    result = facts(case, capsys)
    assert result["kind_numbers"]["hot_overlap_files"] == {"lost_work_by_file": {OVERLAP_STATE: {
        "issues": ["#71"], "rebuilds": 0, "budget_stops": 1, "resolution_seconds": 21}}}
