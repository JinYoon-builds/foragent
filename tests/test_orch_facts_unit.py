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
    """Lane queues live in plan.json, which facts never read; the replay keeps number order."""
    from foragent import orch_queue

    store, root = env
    capacity.write_state(store, 2, NOW)
    approve(store, 1, ["src/*"])
    approve(store, 2, ["src/a.py"])
    approve(store, 3, ["src/b.py"])
    orch_queue.set_order(store, "main", [2, 3, 1])
    facts = collect(store, root, overlap_per_file=1)
    assert facts["resources"]["parallelizable_work"]["held"] == {"2": "structure", "3": "structure"}
    assert facts["binding"]["kind"] == "structure"


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


def test_a_transition_during_the_computation_does_not_reach_the_facts(env, monkeypatch):
    store, root = env
    clock = [NOW - timedelta(seconds=40)]
    store._now = lambda: clock[0]
    store.start_run(6, branch="b", worktree=store.worktree_path(6))
    store.update_run(6, phase="implementing", phase_started_at=clock[0].isoformat())
    clock[0] += timedelta(seconds=10)
    store.update_run(6, phase="checking", phase_started_at=clock[0].isoformat())
    clock[0] += timedelta(seconds=30)  # NOW: checking has taken 30 s.
    read = orch_facts._current_runs
    calls = []

    def racing(store_):
        runs = read(store_)
        if not calls:  # The phase ends right after the run record was read.
            store.update_run(6, phase="reviewing", phase_started_at=clock[0].isoformat())
        calls.append(1)
        return runs

    monkeypatch.setattr(orch_facts, "_current_runs", racing)
    row, = collect(store, root)["runs"]
    assert calls, "The transition happened while facts were computed"
    assert row["phase"] == "checking", "Facts are computed from the frozen read only"
    assert row["checking_seconds"] == 30 and row["test_phase_share"] == pytest.approx(0.75)
    row, = collect(store, root)["runs"]
    assert row["phase"] == "reviewing" and row["checking_seconds"] == 30


def test_open_phase_starts_after_an_event_that_already_closed_it(env, monkeypatch):
    store, root = env
    clock = [NOW - timedelta(seconds=30)]
    store._now = lambda: clock[0]
    store.start_run(6, branch="b", worktree=store.worktree_path(6))
    store.update_run(6, phase="checking", phase_started_at=clock[0].isoformat())
    clock[0] = NOW
    stale = store.get_run(6)
    store.update_run(6, phase="reviewing", phase_started_at=NOW.isoformat())
    # A record read before the transition, with the journal read after it.
    monkeypatch.setattr(orch_facts, "_current_runs", lambda store_: [stale])
    row, = collect(store, root)["runs"]
    assert row["checking_seconds"] == 30


def test_nested_waits_keep_the_work_identity_so_waste_adds_up(env, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from foragent import run_waits

    store, root = env
    clock = [NOW - timedelta(hours=1)]
    store._now = lambda: clock[0]

    @contextmanager
    def queued(ceiling, *, waiting=None):
        waiting()
        clock[0] += timedelta(seconds=20)
        yield

    monkeypatch.setattr(capacity, "hold_test_slot", queued)
    ctx = SimpleNamespace(store=store, config=Config(repo="owner/repo"), now=lambda: clock[0])
    for _ in range(2):
        store.start_run(6, branch="b", worktree=store.worktree_path(6))
        store.update_run(6, phase="baseline", phase_started_at=clock[0].isoformat())
        store.update_run(6, phase_work_id="baseline:abc")
        clock[0] += timedelta(seconds=40)
        with run_waits._test_slot(6, ctx):
            assert store.get_run(6).phase_work_id == "baseline:abc"
            clock[0] += timedelta(seconds=60)
        store.finish_run(6, outcome="timeout")
        clock[0] += timedelta(seconds=5)
    store._now = lambda: NOW
    facts = collect(store, root)
    assert sorted(row["waste_seconds"] for row in facts["runs"]) == [20, 120]
    assert facts["waste"]["seconds"] == 140


def test_recorded_usage_feeds_the_expiring_quota_estimate(env, monkeypatch):
    from foragent import state_local

    store, root = env
    reset = (NOW + timedelta(minutes=10)).isoformat()
    for minutes, used in ((20, 30), (0, 40)):
        store._now = lambda minutes=minutes: NOW - timedelta(minutes=minutes)
        store.record_usage("codex", [{"name": "5h", "used_percent": used, "resets_at": reset}])
    store._now = lambda: NOW
    quota = collect(store, root)["resources"]["quota"]["codex"]
    assert quota["remaining_share"] == pytest.approx(0.6)
    assert quota["expiring_unused_share"] == pytest.approx(0.55)
    monkeypatch.setattr(state_local, "USAGE_HISTORY_LINES", 2)
    store.record_usage("codex", [{"name": "5h", "used_percent": 41, "resets_at": reset}])
    lines = (store.root / "usage-history.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[-1])["windows"][0]["used_percent"] == 41


def test_fa_tidy_done_writes_the_boundary_facts_counts_from(env):
    from foragent import tidy

    store, root = env
    store._now = lambda: NOW - timedelta(hours=2)
    store.start_run(1, branch="b", worktree=store.worktree_path(1))
    store.finish_run(1, outcome="merged")
    store._now = lambda: NOW - timedelta(hours=1)
    tidy.done(store)
    kinds = [json.loads(line)["kind"] for line in (store.root / "orch-events.jsonl").read_text().splitlines()]
    assert "tidy-done" in kinds
    store._now = lambda: NOW - timedelta(minutes=30)
    store.start_run(2, branch="b", worktree=store.worktree_path(2))
    store.finish_run(2, outcome="merged")
    store._now = lambda: NOW
    assert collect(store, root)["merges_since_tidy"] == 1


def test_an_open_recapture_of_recorded_work_is_waste_before_it_closes(env):
    store, root = env
    clock = [NOW - timedelta(hours=1)]
    store._now = lambda: clock[0]
    store.start_run(6, branch="b", worktree=store.worktree_path(6))
    store.update_run(6, phase="baseline", phase_started_at=clock[0].isoformat(), phase_work_id="baseline:abc")
    clock[0] += timedelta(seconds=30)
    store.finish_run(6, outcome="timeout")
    clock[0] = NOW - timedelta(seconds=50)
    store.start_run(6, branch="b", worktree=store.worktree_path(6))
    store.update_run(6, phase="baseline", phase_started_at=clock[0].isoformat(), phase_work_id="baseline:abc")
    store._now = lambda: NOW
    current = [row for row in collect(store, root)["runs"] if row["status"] == "running"]
    assert current[0]["waste_seconds"] == 50, "The open recapture repeats recorded work"


def test_a_restored_open_segment_of_first_work_is_not_waste(env, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from foragent import run_waits

    store, root = env
    clock = [NOW - timedelta(seconds=100)]
    store._now = lambda: clock[0]

    @contextmanager
    def queued(ceiling, *, waiting=None):
        waiting()
        clock[0] += timedelta(seconds=20)
        yield

    monkeypatch.setattr(capacity, "hold_test_slot", queued)
    ctx = SimpleNamespace(store=store, config=Config(repo="owner/repo"), now=lambda: clock[0])
    store.start_run(6, branch="b", worktree=store.worktree_path(6))
    store.update_run(6, phase="baseline", phase_started_at=clock[0].isoformat(), phase_work_id="baseline:abc")
    clock[0] += timedelta(seconds=30)
    with run_waits._test_slot(6, ctx):
        pass  # The restored baseline segment stays open until NOW.
    store._now = lambda: NOW
    row, = collect(store, root)["runs"]
    assert (row["baseline_seconds"], row["waste_seconds"]) == (80, 20)


def test_writers_wait_while_every_input_is_read(env, monkeypatch):
    import fcntl

    store, root = env
    store.start_run(3, branch="b", worktree=store.worktree_path(3))  # Creates this store's state.lock.
    sibling = StateStore(store.root.parent / "other__repo", now=lambda: NOW)
    sibling.start_run(9, branch="b", worktree=sibling.worktree_path(9))
    blocked = []

    def sample():
        # Called while the inputs are read: no writer may take either repository's state lock.
        for path in (store.root / "state.lock", sibling.root / "state.lock"):
            with path.open("a") as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    blocked.append(path.parent.name)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return ("normal", 0.1)

    monkeypatch.setattr(capacity, "machine_load", sample)
    facts = collect(store, root)
    assert sorted(blocked) == ["other__repo", "owner__repo"]
    assert facts["resources"]["parallelizable_work"]["machine_runs"] == 2


def test_warn_memory_does_not_hide_a_fixable_structure_hold(env, monkeypatch):
    store, root = env
    monkeypatch.setattr(capacity, "machine_load", lambda: ("warn", 0.1))
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/shared.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/shared.py"])
    approve(store, 3, ["src/shared.py"])
    facts = collect(store, root)
    assert facts["resources"]["machine"]["memory_headroom"] is True
    assert facts["binding"]["kind"] == "structure" and facts["binding"]["normal"] is False


def test_critical_memory_holds_every_start_like_the_gate(env, monkeypatch):
    store, root = env
    monkeypatch.setattr(capacity, "machine_load", lambda: ("critical", 0.1))
    capacity.write_state(store, 4, NOW)
    approve(store, 2, ["src/a.py"])
    facts = collect(store, root)
    assert facts["resources"]["parallelizable_work"]["held"] == {"2": "machine"}
    assert facts["binding"]["kind"] == "machine" and facts["binding"]["normal"] is True


def test_outside_run_hours_nothing_starts(env):
    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 2, ["src/a.py"])
    hour = NOW.astimezone().hour
    facts = collect(store, root, run_hours=f"{(hour + 1) % 24:02d}-{(hour + 2) % 24:02d}")
    assert facts["resources"]["parallelizable_work"]["held"] == {"2": "run_hours"}
    assert facts["binding"]["kind"] == "run_hours"


def test_facts_use_the_committed_config_the_tick_applied(env):
    from foragent.scheduler_config import record_tick_config

    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/a.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/b.py"])
    local = Config(repo="owner/repo", max_parallel=4)
    assert collect(store, root, max_parallel=4)["binding"] is None
    record_tick_config(store, "origin/main:.foragent.yml", "repo: owner/repo\nmax_parallel: 1\n")
    stamp = (store.root / "tick-config.json").stat().st_mtime_ns
    record_tick_config(store, "origin/main:.foragent.yml", "repo: owner/repo\nmax_parallel: 1\n")
    assert (store.root / "tick-config.json").stat().st_mtime_ns == stamp, "An unchanged config is not rewritten"
    facts = orch_facts.collect(store, local, root)  # collect applies the recorded committed config.
    assert facts["resources"]["parallelizable_work"]["held"] == {"2": "cap"}
    assert facts["binding"]["kind"] == "cap"


def test_a_recorded_github_wait_holds_ready_work(env):
    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 2, ["src/a.py"])
    store.pause_github(NOW + timedelta(minutes=30))
    facts = collect(store, root)
    assert facts["resources"]["parallelizable_work"]["held"] == {"2": "github"}
    assert facts["binding"]["kind"] == "github" and facts["binding"]["normal"] is True


def test_a_running_overlap_wait_is_a_structure_hold(env):
    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/a.py"], labels=("approved", "running"), active=True)
    store.update_run(1, phase="overlap-wait", phase_started_at=NOW.isoformat())
    facts = collect(store, root)
    work = facts["resources"]["parallelizable_work"]
    assert work["in_flight"] == {"1": "structure"} and work["idle_while_waiting"] is True
    assert facts["binding"]["kind"] == "structure"


def test_a_half_written_finish_is_finished_not_queued(env):
    store, root = env
    store.start_run(3, branch="b", worktree=store.worktree_path(3))
    store.update_run(3, phase="test-slot-wait", phase_started_at=NOW.isoformat())
    before = (store.root / "runs" / "3.json").read_bytes()
    store.finish_run(3, outcome="merged")
    (store.root / "runs" / "3.json").write_bytes(before)  # History written, record not yet replaced.
    facts = collect(store, root)
    assert facts["test_slots"]["queue_length"] == 0 and facts["binding"] is None
    row, = facts["runs"]
    assert row["status"] == "finished"


def test_a_close_journaled_twice_counts_once(env, monkeypatch):
    store, root = env
    clock = [NOW - timedelta(seconds=20)]
    store._now = lambda: clock[0]
    store.start_run(3, branch="b", worktree=store.worktree_path(3))
    store.update_run(3, phase="checking", phase_started_at=clock[0].isoformat())
    clock[0] += timedelta(seconds=10)
    write = store._write_run

    def failing(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_run", failing)
    with pytest.raises(OSError):
        store.update_run(3, phase="reviewing", phase_started_at=clock[0].isoformat())
    monkeypatch.setattr(store, "_write_run", write)
    clock[0] += timedelta(seconds=10)
    store.finish_run(3, outcome="merged")
    store._now = lambda: NOW
    row, = collect(store, root)["runs"]
    assert row["checking_seconds"] == 20 and row["runtime_seconds"] == 20
    assert row["test_phase_share"] == pytest.approx(1.0)


def test_a_config_change_during_the_computation_does_not_mix_in(env, monkeypatch):
    from foragent.scheduler_config import record_tick_config

    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/a.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/b.py"])
    record_tick_config(store, "origin/main:.foragent.yml", "repo: owner/repo\nmax_parallel: 4\n")
    read = orch_facts._current_runs

    def racing(store_):
        record_tick_config(store, "origin/main:.foragent.yml", "repo: owner/repo\nmax_parallel: 1\n")
        return read(store_)

    monkeypatch.setattr(orch_facts, "_current_runs", racing)
    assert collect(store, root, max_parallel=4)["binding"] is None, "The frozen read had max_parallel 4"
    monkeypatch.setattr(orch_facts, "_current_runs", read)
    assert collect(store, root, max_parallel=4)["binding"]["kind"] == "cap"


def test_a_sibling_half_written_finish_holds_no_machine_slot(env):
    store, root = env
    sibling = StateStore(store.root.parent / "other__repo", now=lambda: NOW)
    sibling.start_run(9, branch="b", worktree=sibling.worktree_path(9))
    before = (sibling.root / "runs" / "9.json").read_bytes()
    sibling.finish_run(9, outcome="merged")
    (sibling.root / "runs" / "9.json").write_bytes(before)  # History written, record not yet replaced.
    assert collect(store, root)["resources"]["parallelizable_work"]["machine_runs"] == 0


def test_tidy_done_writes_one_boundary_moment(env):
    from foragent import tidy

    store, _ = env
    moments = iter([NOW, NOW + timedelta(seconds=5)])
    store._now = lambda: next(moments)
    tidy.done(store)
    event, = [json.loads(line) for line in (store.root / "orch-events.jsonl").read_text().splitlines()]
    assert event["at"] == store.tidy_state()["last_tidy_at"] == NOW.isoformat()


@pytest.mark.parametrize("seconds,expected_busy", [(100.0, 0.75), (1.0, 0.06)], ids=["fresh", "short-interval"])
def test_observed_load_uses_the_gates_busy_rule(tmp_path, monkeypatch, seconds, expected_busy):
    import os
    from collections import namedtuple

    import psutil

    CPU = namedtuple("CPU", "user system idle")
    (tmp_path / "machine-data").mkdir(exist_ok=True)
    saved = {"cpu_total": 1000.0, "cpu_idle": 900.0, "cpu_busy": 0.06}
    (tmp_path / "machine-data" / capacity.SENSOR_FILE).write_text(json.dumps(saved))
    # Since the saved counters: `seconds` of CPU time, three quarters of it busy.
    monkeypatch.setattr(psutil, "cpu_times", lambda: CPU(100.0 + 0.75 * seconds, 0.0, 900.0 + 0.25 * seconds))
    monkeypatch.setattr(os, "getloadavg", lambda: (8.0, 8.0, 8.0))
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    monkeypatch.delenv("FORAGENT_CPU_BUSY", raising=False)
    before = (tmp_path / "machine-data" / capacity.SENSOR_FILE).read_bytes()
    with capacity.observing():
        sample = capacity.machine_load()
    assert sample.cpu_busy == pytest.approx(expected_busy)
    assert sample[1] == pytest.approx(min(2.0, expected_busy / 0.6))
    assert (tmp_path / "machine-data" / capacity.SENSOR_FILE).read_bytes() == before, "Observing writes nothing"
    monkeypatch.setenv("FORAGENT_CPU_BUSY", "off")
    with capacity.observing():
        sample = capacity.machine_load()
    assert sample[1] == 2.0 and sample.cpu_busy is None


def test_a_sibling_first_reservation_before_locking_is_seen(env, monkeypatch):
    store, root = env
    capacity.write_state(store, 1, NOW)
    approve(store, 5, ["src/a.py"])
    sibling = StateStore(store.root.parent / "new__repo", now=lambda: NOW)
    lock = orch_facts._lock_shared
    reserved = []

    def reserving(stack, path):
        if not reserved:  # Between store discovery and the locks: a new repository reserves a slot.
            sibling.prepare_run_start(9)
            reserved.append(1)
        return lock(stack, path)

    monkeypatch.setattr(orch_facts, "_lock_shared", reserving)
    facts = collect(store, root)
    assert facts["resources"]["parallelizable_work"]["machine_runs"] == 1
    assert facts["binding"]["kind"] == "cap"


def test_an_invalid_recorded_config_falls_back_to_the_checkout(env):
    from foragent.scheduler_config import record_tick_config

    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/a.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/b.py"])
    record_tick_config(store, "origin/main:.foragent.yml", "repo: owner/repo\nmax_parallel: 1\n")
    assert collect(store, root, max_parallel=4)["binding"]["kind"] == "cap"
    record_tick_config(store, "origin/main:.foragent.yml", "max_parallel: [not a number\n")
    facts = collect(store, root, max_parallel=4)
    assert facts["binding"] is None and facts["resources"]["parallelizable_work"]["free_run_slots"] == 3


def test_a_github_wait_holds_before_any_file_check(env):
    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/shared.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/shared.py"])
    approve(store, 3, ["src/shared.py"])
    store.pause_github(NOW + timedelta(minutes=30))
    facts = collect(store, root)
    assert facts["resources"]["parallelizable_work"]["held"] == {"2": "github", "3": "github"}
    assert facts["binding"]["kind"] == "github"


def test_run_hours_hold_before_the_cap(env):
    store, root = env
    capacity.write_state(store, 1, NOW)
    approve(store, 1, ["src/a.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/b.py"])
    hour = NOW.astimezone().hour
    facts = collect(store, root, run_hours=f"{(hour + 1) % 24:02d}-{(hour + 2) % 24:02d}")
    assert facts["resources"]["parallelizable_work"]["held"] == {"2": "run_hours"}
    assert facts["binding"]["kind"] == "run_hours"


def test_critical_memory_ends_the_start_loop_at_the_first_startable_issue(env, monkeypatch):
    store, root = env
    monkeypatch.setattr(capacity, "machine_load", lambda: ("critical", 0.1))
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/shared.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/a.py"])
    approve(store, 3, ["src/shared.py"])
    approve(store, 4, ["src/b.py"])
    # The gate stops at #2; #3 and #4 are never checked in this tick.
    assert collect(store, root, overlap_per_file=1)["resources"]["parallelizable_work"]["held"] == {
        "2": "machine", "3": "machine", "4": "machine"}


@pytest.mark.parametrize("boundary", ["tidy-json", "tidy-done-event"])
def test_legacy_merges_without_times_count_when_started_after_the_tidy(env, boundary):
    store, root = env
    tidy_at = NOW - timedelta(hours=3)
    if boundary == "tidy-json":
        store.update_tidy_state(lambda state: state.update(last_tidy_at=tidy_at.isoformat(), merged_at_tidy=0) or True)
    else:
        append_event(store, tidy_at, None, "tidy-done", "Tidy completed")
    for number, started in ((1, NOW - timedelta(hours=5)), (2, NOW - timedelta(hours=1))):
        store._now = lambda started=started: started
        store.start_run(number, branch="b", worktree=store.worktree_path(number))
        store.finish_run(number, outcome="merged")
    # Legacy history: drop the finish and merge times written by newer code.
    lines = [json.loads(line) for line in (store.root / "runs.jsonl").read_text().splitlines()]
    (store.root / "runs.jsonl").write_text("".join(json.dumps({key: value for key, value in line.items()
                                                                 if key not in ("finished_at", "merged_at")}) + "\n"
                                                   for line in lines))
    store._now = lambda: NOW
    assert collect(store, root)["merges_since_tidy"] == 1, "Only the merge started after the tidy counts"


def test_global_config_is_read_with_the_records(env, monkeypatch, tmp_path):
    from foragent.scheduler_config import record_tick_config

    store, root = env
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "home/.config/foragent/config.yml"
    path.parent.mkdir(parents=True)
    path.write_text("max_parallel: 4\n")
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/a.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/b.py"])
    record_tick_config(store, "origin/main:.foragent.yml", "repo: owner/repo\n")
    read = orch_facts._current_runs

    def racing(store_):
        path.write_text("max_parallel: 1\n")  # The global cap drops after the frozen read.
        return read(store_)

    monkeypatch.setattr(orch_facts, "_current_runs", racing)
    assert collect(store, root)["binding"] is None, "The frozen global config still had max_parallel 4"
    monkeypatch.setattr(orch_facts, "_current_runs", read)
    assert collect(store, root)["binding"]["kind"] == "cap"


def test_checkout_config_is_read_with_the_records(env):
    store, root = env
    capacity.write_state(store, 4, NOW)
    approve(store, 1, ["src/a.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/b.py"])
    (root / ".foragent.yml").write_text("repo: owner/repo\nmax_parallel: 1\n")
    stale = Config(repo="owner/repo", max_parallel=4)
    assert orch_facts.collect(store, stale, root, checkout=True)["binding"]["kind"] == "cap"


def wait_ctx(store, clock):
    from types import SimpleNamespace

    return SimpleNamespace(store=store, config=Config(repo="owner/repo"), now=lambda: clock[0],
                           monotonic=lambda: clock[0].timestamp(),
                           sleep=lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)))


def test_executor_backoff_is_a_wait_phase_not_implementing(env):
    from foragent import run_waits

    store, root = env
    clock = [NOW - timedelta(seconds=250)]
    store._now = lambda: clock[0]
    store.start_run(7, branch="b", worktree=store.worktree_path(7))
    store.update_run(7, phase="implementing", phase_started_at=clock[0].isoformat())
    clock[0] += timedelta(seconds=10)
    run_waits._executor_wait(7, wait_ctx(store, clock), 240)
    assert store.get_run(7).phase == "implementing"
    store.finish_run(7, outcome="timeout")
    store._now = lambda: NOW
    row, = collect(store, root)["runs"]
    assert (row["implementing_seconds"], row["waste_seconds"], row["runtime_seconds"]) == (10, 240, 250)
    assert row["waste_share"] == pytest.approx(0.96)


@pytest.mark.parametrize("held_for", [240, 0])
def test_merge_lock_wait_is_a_wait_phase_only_when_it_waits(env, held_for):
    from foragent import run_waits

    store, root = env
    clock = [NOW - timedelta(seconds=10 + held_for)]
    store._now = lambda: clock[0]
    store.start_run(7, branch="b", worktree=store.worktree_path(7))
    store.update_run(7, phase="merging", phase_started_at=clock[0].isoformat())
    clock[0] += timedelta(seconds=10)

    def acquire():
        clock[0] += timedelta(seconds=held_for)  # Another merge holds the lock this long.

    run_waits._credited_wait(7, wait_ctx(store, clock), acquire, "merge-lock-wait", eager=False)
    assert store.get_run(7).phase == "merging"
    store.finish_run(7, outcome="merged")
    store._now = lambda: NOW
    phases = [json.loads(line)["phase"] for line in (store.root / "orch-events.jsonl").read_text().splitlines()]
    assert phases == (["merging", "merge-lock-wait", "merging"] if held_for else ["merging"])
    row, = collect(store, root)["runs"]
    assert row["waste_seconds"] == held_for and row["runtime_seconds"] == 10 + held_for
    assert row["waste_share"] == pytest.approx(held_for / (10 + held_for))


def test_replay_uses_the_gates_overlap_ranking(env):
    store, root = env
    capacity.write_state(store, 3, NOW)
    approve(store, 1, ["src/shared.py"], labels=("approved", "running"), active=True)
    approve(store, 2, ["src/shared.py"])
    approve(store, 3, ["src/shared.py"])
    approve(store, 4, ["src/other.py"])
    # The gate starts #4 (no overlap) and then #2; the cap, not the files, holds #3.
    facts = collect(store, root)
    assert facts["resources"]["parallelizable_work"]["held"] == {"3": "cap"}
    assert facts["binding"]["kind"] == "cap"


def test_replay_uses_the_global_priority_list(env):
    from foragent import orch_queue

    store, root = env
    capacity.write_state(store, 1, NOW)
    approve(store, 2, ["src/a.py"])
    approve(store, 3, ["src/b.py"], lane="other")
    orch_queue.set_global(store, [3, 2])
    assert collect(store, root)["resources"]["parallelizable_work"]["held"] == {"2": "cap"}


def test_review_only_ci_checks_record_related_tests_as_checking(git_history, tmp_path):
    import sys

    from conftest import BASE_TIME, git_command
    from foragent.ci_mode import review_checks
    from foragent.git import GitRepo
    from foragent.runner import RunContext
    from foragent.tracker import FakeTracker

    git_history.update({
        'src/demo.py': 'VALUE = 1\n',
        'tests/conftest.py': 'import sys\nfrom pathlib import Path\n'
            'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))\n',
        'tests/test_demo.py': 'from demo import VALUE\ndef test_demo(): assert VALUE > 0\n',
        'tests/test_importer.py': 'from demo import VALUE\ndef test_importer(): assert VALUE > 0\n',
    })
    root = git_history.clone
    git_command(root, 'fetch', '-q', 'origin')
    git_command(root, 'merge', '-q', '--ff-only', 'origin/main')
    (root / 'src/demo.py').write_text('VALUE = 2\n')
    git = GitRepo(root)
    git.stage_changes()
    store = StateStore(tmp_path / 'state' / 'owner__repo', now=lambda: BASE_TIME)
    store.start_run(1, branch='test', worktree=root)
    store.update_run(1, phase='reviewing', phase_started_at=BASE_TIME.isoformat())
    config = Config(checks='ci', runner_unit_tests='all', test_command=f'{sys.executable} -m pytest')
    ctx = RunContext('owner/repo', root, FakeTracker(), store, config, git, lambda: BASE_TIME)
    review_checks(1, ctx, git, 'main')
    check, = store.get_run(1).related_tests
    assert check["selected_files"] == ["tests/test_demo.py", "tests/test_importer.py"]
    assert check["changed_file_count"] == 1 and check["timed_out"] is False
    assert set(check["file_seconds"]) == set(check["selected_files"])
    assert store.get_run(1).phase == "reviewing"
    phases = [json.loads(line)["phase"] for line in (store.root / "orch-events.jsonl").read_text().splitlines()]
    assert "checking" in phases


def test_review_head_done_when_is_checking_time(env, monkeypatch):
    from types import SimpleNamespace

    from foragent.issue import ParsedIssue
    from foragent.run_checks import _verify_review_head

    store, root = env
    clock = [NOW - timedelta(seconds=100)]
    store._now = lambda: clock[0]
    store.start_run(8, branch="b", worktree=store.worktree_path(8))
    store.update_run(8, phase="reviewing", phase_started_at=clock[0].isoformat())
    clock[0] += timedelta(seconds=40)
    git = SimpleNamespace(root=root, stage_changes=lambda: None, head_sha=lambda: "head",
                          has_staged_changes=lambda: False)
    ctx = SimpleNamespace(store=store, config=Config(repo="owner/repo"), now=lambda: clock[0],
                          monotonic=lambda: clock[0].timestamp())
    from foragent import runner
    real = runner._check

    def timed(*args, **kwargs):
        clock[0] += timedelta(seconds=60)
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "_check", timed)
    assert _verify_review_head(8, ctx, git, ParsedIssue(None, "", "", ["true"], []), "head", "") is None
    store.finish_run(8, outcome="merged")
    store._now = lambda: NOW
    row, = collect(store, root)["runs"]
    assert row["checking_seconds"] == 60 and row["test_phase_share"] == pytest.approx(0.6)
