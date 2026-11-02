"""Locked acceptance for flow-scorecard-split.

Synthetic history enters through public StateStore methods. The CLI, interval
calculation and rendering are real. Expectations are hand-calculated minutes.
"""

from datetime import timedelta
import json
import subprocess

import pytest

from foragent.state import StateStore
from test_scorecard_flow import (
    NOW, flow, _approved, _budget, _merge, _phased, _read,  # noqa: F401
)
from test_skills_autosync import isolated_home  # noqa: F401


PARTS = {"work", "concurrency_cost", "machine_limit", "ordering", "waste", "unknown"}


def write(case, kind, number=41, **data):
    writer = getattr(case.store, "record_flow", None)
    assert callable(writer), "The record issues must supply the shared public storage contract"
    run = case.store.get_run(number)
    writer(kind, issue=number, run_started_at=run.started_at if run else None, **data)


def split(card):
    today = card["window"]["today"]
    assert "approval_to_merge" in today, "Expose the five parts plus unknown in the public scorecard"
    value = today["approval_to_merge"]
    assert set(value["parts"]) == PARTS
    assert all(type(n) in (int, float) and n >= 0 for n in value["parts"].values())
    assert sum(value["parts"].values()) == pytest.approx(value["total_minutes"])
    return value


def test_five_parts_use_changed_facts_and_keep_closed_issue_history(flow, capsys):
    base = NOW - timedelta(hours=2)
    _approved(flow, 41, base)
    _budget(flow, base)
    flow.store.update_local_issue(41, blocked_by=[{"number": 40, "state": "open", "repository": None}])

    def at(minutes):
        flow.clock[0] = base + timedelta(minutes=minutes)

    def ready(minutes, reason, occupied=0, waiters=0):
        at(minutes)
        write(flow, "ready", reason=reason, outcome="waiting",
              run_slots={"limit": 1, "occupied": occupied, "merge_waiters": waiters})

    # 4 ordering + 5 machine + 3 waiter-held + 3 held-no-retry + 3 tick delay.
    at(4)
    flow.store.update_local_issue(41, blocked_by=[])
    ready(4, "slots-full", occupied=1)
    ready(9, "slots-full", occupied=1, waiters=1)
    at(12)
    flow.store.update_local_issue(41, labels=["approved", "blocked"])
    ready(12, "held-no-retry")
    at(15)
    flow.store.update_local_issue(41, labels=["approved", "ready"])
    ready(15, "free-slot")
    at(18)
    flow.store.start_run(41, branch="foragent/issue-41", worktree=flow.store.worktree_path(41))
    write(flow, "ready", reason=None, outcome="started")

    # 21 work, 17 concurrency, 5 queue waste, 2 unknown.
    for start, end, phase, cause in [
        (18, 24, "implementing", "initial"),
        (24, 27, "reviewing", "initial"),
        (27, 31, "checking", "initial"),
        (31, 35, "ci", "initial"),
        (35, 38, "resolving-conflict", "base-changed"),
        (38, 42, "checking", "base-changed"),
        (42, 45, "ci", "base-changed"),
        (45, 48, "checking", "group-failed"),
        (48, 52, "overlap-wait", "file-overlap"),
        (52, 57, "merge-lock-wait", "queue"),
        (57, 59, "unclassified", None),
        (59, 63, "merging", "initial"),
    ]:
        at(start)
        flow.store.update_run(41, phase=phase, phase_started_at=flow.clock[0].isoformat())
        at(end)
        if cause is not None:
            write(flow, "activity", activity=phase, cause=cause,
                  started_at=(base + timedelta(minutes=start)).isoformat(),
                  finished_at=flow.clock[0].isoformat())
    at(63)
    flow.store.record_merge(41, flow.clock[0])
    flow.store.finish_run(41, outcome="merged")
    flow.store.update_local_issue(41, state="closed", labels=["approved"], merged=True,
                                  merged_at=flow.clock[0].isoformat())
    flow.clock[0] = NOW
    card = _read(flow, capsys)
    value = split(card)
    assert value["total_minutes"] == pytest.approx(63)
    assert value["parts"] == pytest.approx({
        "work": 21, "concurrency_cost": 17, "machine_limit": 5,
        "ordering": 4, "waste": 14, "unknown": 2,
    })
    # Latest dependencies/labels now say nothing about the first 18 minutes.
    flow.store.ingest([], NOW, {}, complete=True)
    flow.store = StateStore(flow.store.root, now=lambda: flow.clock[0])
    flow.ctx.store = flow.store
    assert split(_read(flow, capsys)) == value


def test_missing_history_is_unknown_and_fractional_minutes_are_not_forced_into_a_part(flow, capsys):
    base = NOW - timedelta(hours=1)
    _approved(flow, 41, base)
    # No evidence explains 29.5 seconds before the run. The phase explains 30.5s.
    start = base + timedelta(seconds=29.5)
    flow.clock[0] = start
    flow.store.start_run(41, branch="foragent/issue-41", worktree=flow.store.worktree_path(41))
    flow.store.update_run(41, phase="implementing", phase_started_at=start.isoformat())
    flow.clock[0] = base + timedelta(minutes=1)
    flow.store.record_merge(41, flow.clock[0])
    flow.store.finish_run(41, outcome="merged")
    flow.clock[0] = NOW
    value = split(_read(flow, capsys))
    assert value["total_minutes"] == pytest.approx(1)
    assert value["parts"]["work"] == pytest.approx(30.5 / 60)
    assert value["parts"]["unknown"] == pytest.approx(29.5 / 60)
    assert sum(value["parts"][k] for k in PARTS - {"work", "unknown"}) == 0


def test_slot_idle_measures_run_and_test_slots_separately_and_names_inside_run_waits(flow, capsys):
    start = NOW - timedelta(minutes=20)
    _approved(flow, 41, start)
    _budget(flow, start, parallel=1, slots=2)
    _phased(flow, 90, start, [("checking", 20)])
    flow.clock[0] = NOW
    card = _read(flow, capsys)
    today = card["window"]["today"]
    assert today.get("run_slot_idle_minutes") == 0, "The only run slot stayed occupied"
    assert today.get("test_slot_idle_minutes") == pytest.approx(20)
    text = _read(flow, capsys, human=True).lower()
    assert "run slot" in text and "test slot" in text
    assert "inside runs" in text, "Wait-by-cause does not include first-start or between-run gaps"


def test_time_to_merge_starts_at_latest_approval_and_reading_does_no_io(flow, capsys, monkeypatch):
    merge = NOW - timedelta(hours=1)
    _approved(flow, 41, merge - timedelta(minutes=90))
    _merge(flow, 41, merge, runtime=10, finish_delay=20)
    flow.clock[0] = NOW
    # A bare turn result is an attempted API operation, never proof of success.
    (flow.store.root / "merge-queue.jsonl").write_text(json.dumps({
        "n": 99, "event": "turn", "result": "merged", "at": merge.isoformat(),
        "started_at": (merge - timedelta(minutes=5)).isoformat(), "target": "main",
    }) + "\n")

    def forbidden(*args, **kwargs):
        pytest.fail("Scorecard reads must not invoke a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    card = _read(flow, capsys)
    today = card["window"]["today"]
    assert today["median_minutes_to_merge"] == pytest.approx(90), "Run start and cleanup are not approval/merge"
    assert today["lead_time_minutes"] == pytest.approx(90)
    assert today["merges"] == 1
    assert split(card)["total_minutes"] == pytest.approx(90)
    text = _read(flow, capsys, human=True).lower()
    assert "approval" in text and "unknown" in text
