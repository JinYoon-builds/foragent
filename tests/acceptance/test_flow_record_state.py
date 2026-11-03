"""Locked acceptance for flow-record-state. Public producers and storage contract.

The two new StateStore methods are the order's inter-issue contract, not a
choice of file format. All other production calls are existing public entries.
"""

from datetime import UTC, datetime, timedelta

import pytest

from foragent import orch_queue, orch_tick_outcome, scheduler
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import Issue
from test_scheduler import ctx  # noqa: F401
from test_lane_width_by_overlap import _finish, _wide_lane
from test_optimistic_overlap import _issue, offline_overlap  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


NOW = datetime(2026, 11, 1, 9, tzinfo=UTC)


def records(store, kind=None, issue=None, **window):
    reader = getattr(store, "flow_records", None)
    assert callable(reader), "Missing public local flow_records(since=None, until=None)"
    rows = reader(**window)
    for row in rows:
        assert {"at", "kind", "issue", "run_started_at", "data"} <= row.keys()
        assert datetime.fromisoformat(row["at"]).utcoffset() == timedelta(0)
        assert isinstance(row["data"], dict)
    return [r for r in rows if (kind is None or r["kind"] == kind)
            and (issue is None or r["issue"] == issue)]


def test_label_and_dependency_changes_survive_close_absence_and_reload(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    issue = Issue(41, "Task", "", frozenset({"approved", "ready"}), "open")
    open_dep = {"number": 40, "state": "open", "repository": None}

    def observe(labels, deps, state="open"):
        nonlocal issue
        from dataclasses import replace

        issue = replace(issue, labels=frozenset(labels), state=state)
        store.refresh_local_issues([issue], [], {}, clock[0], blocked_by={41: deps})

    observe(["approved", "ready"], [open_dep])
    for _ in range(2):
        clock[0] += timedelta(seconds=1)
        observe(["ready", "approved"], [dict(open_dep)])
    clock[0] += timedelta(seconds=1)
    observe(["approved", "blocked"], [open_dep])
    clock[0] += timedelta(seconds=1)
    observe(["approved", "ready"], [])
    clock[0] += timedelta(seconds=1)
    observe(["approved"], [], "closed")
    before = records(store, "issue-state", 41)
    assert len(before) == 4, "Initial facts plus three changes; repeated reads add nothing"
    assert [(r["data"]["labels"], r["data"]["blocked_by"], r["data"]["state"])
            for r in before] == [
        (["approved", "ready"], [open_dep], "open"),
        (["approved", "blocked"], [open_dep], "open"),
        (["approved", "ready"], [], "open"),
        (["approved"], [], "closed"),
    ]
    clock[0] += timedelta(seconds=1)
    store.ingest([], clock[0], {}, complete=True)
    reloaded = StateStore(store.root, now=lambda: clock[0])
    assert records(reloaded, "issue-state", 41)[:4] == before
    # A late result must not rewrite the earlier accepted history.
    reloaded.ingest([issue], NOW - timedelta(seconds=1), {})
    assert records(reloaded, "issue-state", 41)[:4] == before


def test_ready_wait_is_once_per_change_and_start_control_keeps_its_behavior(ctx):
    starts = _wide_lane(ctx, 1)
    clock = [ctx.now()]
    ctx.now = lambda: clock[0]
    _issue(ctx, 1, ["src/busy.py"], running=True)
    _issue(ctx, 2, ["src/free.py"])
    for _ in range(3):
        assert scheduler.tick(ctx).skipped is None
        clock[0] += timedelta(seconds=1)
    assert starts == [] and ctx.store.get_run(2) is None
    rows = records(ctx.store, "ready", 2)
    assert len(rows) == 1 and rows[0]["data"]["reason"] == "slots-full"
    assert rows[0]["data"]["run_slots"] == {
        "limit": 1, "occupied": 1, "merge_waiters": 0,
    }
    _finish(ctx, 1)
    assert scheduler.tick(ctx).skipped is None
    assert starts == [2], "Recording must preserve the normal first available start"
    assert ctx.store.get_run(2).status == "running"
    rows = records(ctx.store, "ready", 2)
    assert [r["data"]["reason"] for r in rows] == ["slots-full", None]
    assert rows[-1]["data"]["outcome"] == "started"


def test_priority_changes_and_tick_durations_are_durable_without_changing_results(tmp_path, monkeypatch):
    clock, mono = [NOW], [0.0]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    monkeypatch.setattr(orch_tick_outcome.time, "monotonic", lambda: mono[0])
    for order in ([7, 3], [7, 3, 7], [3, 7], []):
        assert orch_queue.set_global(store, order) == list(dict.fromkeys(order))
        clock[0] += timedelta(seconds=1)
    def tick():
        mono[0] += 2.5
        return "unchanged-result"

    assert orch_tick_outcome.run_recorded(store, Config(), "owner/repo", tick) == "unchanged-result"
    clock[0] += timedelta(seconds=3)

    def failed():
        mono[0] += 1.5
        raise ValueError("fixture failure")

    with pytest.raises(ValueError, match="fixture failure"):
        orch_tick_outcome.run_recorded(store, Config(), "owner/repo", failed)
    priorities = records(store, "priority")
    assert [r["data"]["issues"] for r in priorities] == [[7, 3], [3, 7], []]
    rows = records(StateStore(store.root), "tick")
    assert [(r["data"]["status"], r["data"]["duration_seconds"]) for r in rows] == [
        ("ok", 2.5), ("failed", 1.5),
    ], "Tick facts are per completed/failed tick, even when timing repeats"
    assert records(store, since=NOW, until=NOW + timedelta(seconds=1)) == priorities[:1]


def test_record_retention_bounds_rows_and_bytes_and_keeps_recent_closed_history(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    writer = getattr(store, "record_flow", None)
    assert callable(writer), "Missing public record_flow inter-issue contract"
    for n in range(2200):
        writer("priority", issues=[n], fixture="x" * 2048)
        clock[0] += timedelta(seconds=1)
    store.update_local_issue(41, labels=["approved"], state="closed", blocked_by=[])
    reloaded = StateStore(store.root)
    rows = records(reloaded)
    assert 0 < len(rows) <= 2048, "All retained generations together have a finite row bound"
    assert sum(p.stat().st_size for p in store.root.rglob("*") if p.is_file()) <= 8 * 1024 * 1024
    assert records(reloaded, "priority")[-1]["data"]["issues"] == [2199]
    assert records(reloaded, "issue-state", 41)[-1]["data"]["state"] == "closed"
