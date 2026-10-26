"""Boundary cases for fa orch facts: the facts follow the gate's own admission rules."""

from datetime import UTC, datetime, timedelta
import json

import pytest

from foragent import capacity, orch_facts
from foragent.config import Config
from foragent.issue import body_hash
from foragent.orch_events import append_event
from foragent.state import StateStore

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.1))
    store = StateStore(tmp_path / "data" / "owner__repo", now=lambda: NOW)
    store.root.mkdir(parents=True)
    (tmp_path / "repo").mkdir()
    # A root entry lets the gate's read-only case-sensitivity probe answer; empty roots assume overlap.
    (tmp_path / "repo" / "README.md").write_text("x\n")
    return store, tmp_path / "repo"


def approve(store, number, files, *, labels=("approved", "ready"), lane="main", active=False):
    body = ("## Goal\nChange.\n## Files\n" + "".join(f"- {path}\n" for path in files)
            + "## Done when\n```sh\ntrue\n```\n")
    store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=(NOW - timedelta(hours=1)).isoformat())
    store.update_local_issue(number, title=f"Issue {number}", labels=list(labels), state="open", lane=lane)
    if active:
        store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number), lane=lane)


def collect(store, root, **config):
    return orch_facts.collect(store, Config(repo="owner/repo", **config), root)


def test_admitted_test_slots_include_the_machine_default_ceiling(env):
    store, root = env
    slots = collect(store, root, test_slots=4)["resources"]["test_slots"]
    assert slots["current"] == 4 and slots["admitted"] == capacity.effective_test_slots(4) == 2


def test_overlap_within_overlap_per_file_is_not_a_hold(env):
    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/shared.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/shared.py"])
    facts = collect(store, root)
    work = facts["resources"]["parallelizable_work"]
    assert work["held"] == {} and work["idle_while_waiting"] is False
    assert facts["binding"] is None
    approve(store, 3, ["src/shared.py"])
    facts = collect(store, root)
    assert facts["resources"]["parallelizable_work"]["held"] == {"3": "structure"}
    assert facts["binding"]["kind"] == "structure"


def test_exclusive_declaration_holds_a_sharing_issue(env):
    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/shared.py"], labels=("approved", "running"), active=True)
    body = ("<!-- foragent:v1 -->\nexclusive:\n- src/shared.py\n## Goal\nChange.\n## Files\n- src/shared.py\n"
            "## Done when\n```sh\ntrue\n```\n")
    store.save_approval(2, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=NOW.isoformat())
    store.update_local_issue(2, title="Issue 2", labels=["approved", "ready"], state="open")
    assert collect(store, root)["resources"]["parallelizable_work"]["held"] == {"2": "structure"}


def test_sibling_repository_runs_use_the_machine_cap(env):
    store, root = env
    capacity.write_state(store, 2, NOW)
    sibling = StateStore(store.root.parent / "other__repo", now=lambda: NOW)
    for number in (1, 2):
        sibling.start_run(number, branch=f"b{number}", worktree=sibling.worktree_path(number))
    approve(store, 5, ["src/a.py"])
    facts = collect(store, root)
    work = facts["resources"]["parallelizable_work"]
    assert (work["machine_runs"], work["free_run_slots"], work["held"]) == (2, 0, {"5": "cap"})
    assert facts["binding"]["kind"] == "cap"


def test_quota_binds_only_through_a_lane_that_needs_the_paused_executor(env):
    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 5, ["src/a.py"])
    # Main uses codex and claude; a paused unrelated executor holds nothing.
    store.pause_executor("gemini", NOW + timedelta(hours=1))
    facts = collect(store, root)
    assert facts["resources"]["quota"]["gemini"]["paused"] is True, "A pause without usage records is still a fact"
    assert facts["binding"] is None
    store.pause_executor("codex", NOW + timedelta(hours=1))
    facts = collect(store, root)
    assert facts["resources"]["parallelizable_work"]["held"] == {"5": "quota"}
    assert facts["binding"]["kind"] == "quota" and facts["binding"]["normal"] is True


def test_merges_since_tidy_event_use_the_merge_time_not_the_finish(env):
    store, root = env
    clock = [NOW - timedelta(hours=3)]
    store._now = lambda: clock[0]
    store.start_run(1, branch="b", worktree=store.worktree_path(1))
    store.record_merge(1, NOW - timedelta(hours=2, minutes=50))
    clock[0] = NOW - timedelta(hours=2)  # Cleanup finishes after the tidy below.
    store.finish_run(1, outcome="merged")
    store._now = lambda: NOW
    append_event(store, NOW - timedelta(hours=2, minutes=30), None, "tidy-done", "Tidy completed")
    assert collect(store, root)["merges_since_tidy"] == 0


def test_tidy_done_count_matches_the_tidy_schedule(env):
    from foragent import tidy

    store, root = env
    for hours in (3, 2):
        store._now = lambda hours=hours: NOW - timedelta(hours=hours)
        store.start_run(1, branch="b", worktree=store.worktree_path(1))
        store.finish_run(1, outcome="merged")
    store._now = lambda: NOW
    tidy.done(store)
    assert collect(store, root)["merges_since_tidy"] == tidy.status(store)["merges_since"] == 0
    (store.root / "orch-events.jsonl").write_text(json.dumps(
        {"at": (NOW - timedelta(days=1)).isoformat(), "issue": None, "kind": "tidy-done", "summary": "old"}) + "\n")
    assert collect(store, root)["merges_since_tidy"] == 0, "The newer `fa tidy done` record wins"


def test_replay_follows_the_lane_queue_order(env):
    from foragent import orch_queue

    store, root = env
    capacity.write_state(store, 2, NOW)
    approve(store, 1, ["src/*"])
    approve(store, 2, ["src/a.py"])
    approve(store, 3, ["src/b.py"])
    orch_queue.set_order(store, "main", [2, 3, 1])
    facts = collect(store, root, overlap_per_file=1)
    assert facts["resources"]["parallelizable_work"]["held"] == {"1": "cap"}
    assert facts["binding"]["kind"] == "cap"


def test_replay_applies_the_gates_recorded_union_answers(env):
    from foragent.overlap_files import record_union

    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/shared.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/shared.py"])
    approve(store, 3, ["src/shared.py"])
    assert collect(store, root)["resources"]["parallelizable_work"]["held"] == {"3": "structure"}
    record_union(store, {"src/shared.py"}, {"src/shared.py"})
    facts = collect(store, root)
    assert facts["resources"]["parallelizable_work"]["held"] == {} and facts["binding"] is None


def phase_events(store):
    path = store.root / "orch-events.jsonl"
    return [event for line in path.read_text().splitlines()
            if (event := json.loads(line))["kind"] == "phase"] if path.exists() else []


def test_phase_transitions_and_finish_journal_seconds(env, monkeypatch):
    import os

    store, _ = env
    monkeypatch.setattr(os, "getloadavg", lambda: (1.5, 1.0, 1.0))
    clock = [NOW]
    store._now = lambda: clock[0]
    store.start_run(4, branch="b", worktree=store.worktree_path(4))
    store.update_run(4, phase="implementing", phase_started_at=clock[0].isoformat())
    clock[0] += timedelta(seconds=30)
    store.update_run(4, phase="checking", phase_started_at=clock[0].isoformat())
    clock[0] += timedelta(seconds=5)
    store.finish_run(4, outcome="merged")
    assert [(e["phase"], e["seconds"], e["load"]) for e in phase_events(store)] == [
        ("implementing", 30, 1.5), ("checking", 5, 1.5)]


def test_repeated_work_links_to_its_first_phase_across_executions(env):
    store, _ = env
    clock = [NOW]
    store._now = lambda: clock[0]
    for _ in range(2):
        store.start_run(4, branch="b", worktree=store.worktree_path(4))
        store.update_run(4, phase="baseline", phase_started_at=clock[0].isoformat(), phase_work_id="baseline:abc")
        clock[0] += timedelta(seconds=10)
        store.finish_run(4, outcome="timeout")
        clock[0] += timedelta(seconds=10)
    first, second = phase_events(store)
    assert first["work_id"] == second["work_id"] == "baseline:abc"
    assert "repeat_of" not in first and second["repeat_of"] == first["event_id"]


def test_queued_test_slot_is_its_own_phase(env, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from foragent import run_waits

    store, root = env
    clock = [NOW]
    store._now = lambda: clock[0]
    store.start_run(4, branch="b", worktree=store.worktree_path(4))
    store.update_run(4, phase="checking", phase_started_at=clock[0].isoformat())
    seen = []

    @contextmanager
    def queued(ceiling, *, waiting=None):
        clock[0] += timedelta(seconds=3)
        waiting()
        seen.append(store.get_run(4).phase)
        clock[0] += timedelta(seconds=20)
        yield

    monkeypatch.setattr(capacity, "hold_test_slot", queued)
    ctx = SimpleNamespace(store=store, config=Config(repo="owner/repo"), now=lambda: clock[0])
    with run_waits._test_slot(4, ctx):
        assert store.get_run(4).phase == "checking"
    assert seen == ["test-slot-wait"]
    assert [(e["phase"], e["seconds"]) for e in phase_events(store)] == [("checking", 0), ("test-slot-wait", 23)]
    facts = orch_facts.collect(store, Config(repo="owner/repo"), root)
    row, = facts["runs"]
    assert row["test_slot_wait_seconds"] == 23 and facts["test_slots"]["total_wait_seconds"] == 23
