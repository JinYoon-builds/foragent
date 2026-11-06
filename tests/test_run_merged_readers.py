"""Every reader asks "did this run merge" through the store's one rule (#1836)."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from foragent import scheduler  # noqa: F401  (loads scheduler_recovery in its import order)
from foragent import merge_owner, orch_facts_kinds, scheduler_recovery, star
from foragent.state import StateStore
from foragent.state_runs import record_merged
from foragent.watch_sections import progress

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _store(tmp_path, clock):
    return StateStore(tmp_path / "state", now=lambda: clock[0])


def _queued(store, clock, number, *, finished, merge=True, write_snapshot=True):
    clock[0] = finished - timedelta(minutes=10)
    run = store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    clock[0] = finished
    store.finish_run(number, outcome="merge-queued")
    if merge:
        fact = {
            "pr": 1000 + number, "target": "main", "head": f"{number:040x}",
            "merged_at": (finished + timedelta(minutes=1)).isoformat()}
        if write_snapshot:
            store.record_owner_merge(number, run.started_at, fact)
        else:
            with patch.object(store, "_write_json", side_effect=OSError("snapshot interrupted")):
                with pytest.raises(OSError, match="snapshot interrupted"):
                    store.record_owner_merge(number, run.started_at, fact)
    return run


def test_merged_work_counts_owner_merges_and_not_queued_runs(tmp_path):
    clock = [NOW]
    store = _store(tmp_path, clock)
    for number in (1, 2, 3):
        _queued(store, clock, number, finished=NOW - timedelta(hours=number))
    _queued(store, clock, 4, finished=NOW - timedelta(minutes=5), merge=False)
    clock[0] = NOW
    work = progress.merged_work(store)
    assert [number for number, *_ in work] == [1, 2, 3], "Owner merges are ETA samples; a queued run is not"
    assert work[0][3] == NOW - timedelta(hours=1) + timedelta(minutes=1), "Dated by the owner's merged_at"


def test_owner_merge_time_survives_an_interrupted_snapshot_write(tmp_path):
    clock = [NOW]
    store = _store(tmp_path, clock)
    run = _queued(store, clock, 1, finished=NOW - timedelta(hours=10), write_snapshot=False)
    clock[0] = NOW
    assert store.run_merged(store.read_history()[0])
    assert store.merge_times()[(1, run.started_at)] == (NOW - timedelta(hours=10, minutes=-1)).isoformat()


def test_merge_stall_dates_owner_merges_without_their_history_rows(tmp_path):
    clock = [NOW]
    store = _store(tmp_path, clock)
    expected = {}
    # Worker finishes long ago; the owner merged both just now, but neither history row landed.
    for number, minutes in ((1, 12), (2, 2)):
        run = _queued(store, clock, number, finished=NOW - timedelta(hours=10, minutes=minutes), merge=False)
        fact = {"pr": number, "target": "main", "head": f"{number:040x}",
                "merged_at": (NOW - timedelta(minutes=minutes)).isoformat()}
        path = store.root / "runs" / f"{number}.json"
        value = json.loads(path.read_text())
        value.pop("run_generation", None)  # Legacy writers saved the snapshot before history.
        value.update(owner_merge=fact, owner_merge_started_at=run.started_at, merged_at=fact["merged_at"])
        path.write_text(json.dumps(value))
        expected[number, run.started_at] = fact["merged_at"]
    store.update_local_issue(90, title="Queued work", labels=[], state="open", body="")
    clock[0] = NOW
    assert store.merge_times() == expected
    assert orch_facts_kinds.merge_stall_numbers(store, NOW, []) == {}, (
        "The owner merged two minutes ago: no stall, whatever the worker's finish time says")


def test_malformed_run_identities_count_nothing_and_raise_nothing(tmp_path):
    assert not record_merged({"number": [], "outcome": "merge-queued"}, {})
    assert not record_merged({"number": 1, "started_at": [], "outcome": "merge-queued"}, {(1, "x"): {}})
    assert not record_merged(["not", "a", "record"], {})
    root = tmp_path / "machine"
    for repo, rows in (("a__one", [{"number": 1, "started_at": "s", "outcome": "merged"}]), ("b__two", [])):
        (root / repo / "runs").mkdir(parents=True)
        (root / repo / "runs.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    (root / "b__two" / "runs" / "2.json").write_text(json.dumps({"number": [], "started_at": {}}))
    (root / "a__one" / "runs" / "3.json").write_text(json.dumps([1, 2]))
    assert star.local_merges(root) == 1


def test_dead_worker_with_owner_admission_waits_even_with_the_switch_off(tmp_path, monkeypatch):
    clock = [NOW]
    store = _store(tmp_path, clock)
    store.start_run(5, branch="foragent/issue-5", worktree=store.worktree_path(5))
    monkeypatch.setattr(merge_owner, "pending", lambda store, number: number == 5)
    monkeypatch.setattr(scheduler_recovery, "registered_since", lambda ctx, run: False)
    removed, labels = [], []
    ctx = SimpleNamespace(
        store=store, config=SimpleNamespace(merge_owner=False),
        git=SimpleNamespace(remove_worktree=removed.append),
        tracker=SimpleNamespace(remove_labels=lambda number, names: labels.append((number, names))))
    scheduler_recovery._finish_dead_run(ctx, store.get_run(5), SimpleNamespace(actions=[]))
    run = store.get_run(5)
    assert run.outcome == "merge-queued", "A recorded owner admission is no crash after the switch is turned off"
    assert labels and labels[0][0] == 5


def test_orphan_label_after_a_late_handoff_is_no_crash(tmp_path, monkeypatch):
    """The worker handed off after the tick's earlier checks; the repair's own read still sees it."""
    from foragent.labels import APPROVED, RUNNING
    from foragent.tracker import Issue

    clock = [NOW]
    store = _store(tmp_path, clock)
    store.start_run(6, branch="foragent/issue-6", worktree=store.worktree_path(6))
    store.finish_run(6, outcome="merge-queued")
    queued = store.get_run(6)
    monkeypatch.setattr(merge_owner, "pending", lambda store, number: number == 6)
    labels = []
    issue = Issue(6, "Queued", "", frozenset({RUNNING, APPROVED}), "open")
    ctx = SimpleNamespace(
        store=store, config=SimpleNamespace(merge_owner=False),
        git=SimpleNamespace(remove_worktree=lambda path: None),
        tracker=SimpleNamespace(remove_labels=lambda number, names: labels.append((number, names)),
                                get_issue=lambda number: issue, add_labels=lambda number, names: None))
    scheduler_recovery._recover_orphan(ctx, issue, SimpleNamespace(actions=[]))
    run = store.get_run(6)
    assert (run.outcome, run.started_at) == ("merge-queued", queued.started_at), (
        "A handed-off generation keeps its admission: no new crashed generation")
    assert labels and labels[0][0] == 6
