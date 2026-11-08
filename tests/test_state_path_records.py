"""Lifetime retention, interrupted rotation and independent phase identities."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import orch_events, orch_scorecard, orch_scorecard_records, order_prepare, scheduler, state_logs
from foragent.state import StateStore
from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as owner_support
from helpers.merge_batch import batched, queue_workers  # noqa: F401


@pytest.fixture
def store(tmp_path):
    clock = [datetime(2026, 11, 7, tzinfo=UTC)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.clock = clock
    return store


def test_archived_issue_rows_keep_written_order_duplicates_and_change_memory(store, monkeypatch):
    monkeypatch.setattr(state_logs, "FLOW_GENERATION_LINES", 2)
    store.record_flow_change("ready", issue=1, reason="free-slot", outcome="waiting")
    # Equal timestamps and even equal rows remain separate observations when written separately.
    store.record_flow("activity", issue=1, cause="initial")
    store.record_flow("activity", issue=1, cause="initial")
    expected = store.flow_records()
    for index in range(8):
        store.record_flow("tick", index=index)
    restarted = StateStore(store.root, now=store._now)
    assert [row for row in restarted.flow_records() if row["issue"] == 1] == expected
    restarted.record_flow_change("ready", issue=1, reason="free-slot", outcome="waiting")
    assert [row for row in restarted.flow_records() if row["issue"] == 1] == expected
    assert restarted.flow_records(until=store.clock[0]) == []


@pytest.mark.parametrize("fresh_age", [timedelta(days=1), state_logs.JOURNAL_RETENTION])
def test_housekeeping_expires_finished_archives_by_each_issues_newest_observation(store, monkeypatch, fresh_age):
    monkeypatch.setattr(state_logs, "FLOW_GENERATION_LINES", 2)
    monkeypatch.setattr(store, "_issue_record", lambda: {
        "issues": {str(n): {"number": n, "state": "closed"} for n in (1, 2, 3, 4)}})
    now = store.clock[0]
    store.clock[0] = now - timedelta(days=60)
    # The first archive expires entirely; the second mixes expired and live history.
    for issue, tag in [(1, "first"), (1, "second"), (1, "third"), (2, "first"),
                       (3, "first"), (4, "first")]:
        store.record_flow("ready", issue=issue, tag=tag)
    store.clock[0] = now - fresh_age
    # The live issues' newest observations are in an archive, the old generation,
    # and the current generation, respectively. All three must protect old history.
    for issue in (2, 3, 4):
        store.record_flow("ready", issue=issue, tag="latest")
        store.record_flow("tick", status="ok")
    archives = sorted((store.root / state_logs.FLOW_RETAINED).glob("*.jsonl"))
    assert len(archives) == 4
    generations = {name: (store.root / name).read_bytes()
                   for name in (state_logs.FLOW_OLD_FILE, state_logs.FLOW_FILE)}
    before = store.flow_records()
    store.clock[0] = now
    changed = store.trim_journals(now)
    assert store.flow_records() == [row for row in before if row["issue"] != 1]
    assert set(changed) == {archives[0], archives[1]}
    assert not archives[0].exists()
    assert len(archives[1].read_bytes().splitlines()) == 1
    assert {name: (store.root / name).read_bytes() for name in generations} == generations
    assert store.trim_journals(now) == []

    # When observations stop, the remaining issues expire as whole archived histories.
    store.clock[0] += state_logs.JOURNAL_RETENTION + timedelta(days=1)
    store.trim_journals(store.clock[0])
    assert not list((store.root / state_logs.FLOW_RETAINED).glob("*.jsonl"))
    assert {name: (store.root / name).read_bytes() for name in generations} == generations


def test_interrupted_archive_transfer_loses_no_accepted_row_and_does_not_duplicate_it(store, monkeypatch, caplog):
    monkeypatch.setattr(state_logs, "FLOW_GENERATION_LINES", 2)
    store.record_flow("ready", issue=1, outcome="waiting")
    for index in range(3):
        store.record_flow("tick", index=index)
    expected = store.flow_records()
    write = store._atomic_write

    def fail_archive(path, content):
        if path.parent.name == state_logs.FLOW_RETAINED and path.suffix == ".jsonl":
            raise OSError("archive disk unavailable")
        return write(path, content)

    monkeypatch.setattr(store, "_atomic_write", fail_archive)
    store.record_flow("tick", index=3)
    assert "Flow record write failed" in caplog.text
    assert store.flow_records() == expected  # The rename succeeded; the rewrite did not.
    monkeypatch.setattr(store, "_atomic_write", write)
    store.record_flow("tick", index=3)
    assert store.flow_records() == [*expected, store.flow_records()[-1]]
    assert store.flow_records()[-1]["data"] == {"index": 3}


def test_archives_are_readable_with_a_relative_store_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(state_logs, "FLOW_GENERATION_LINES", 1)
    store = StateStore("state")
    store.record_flow("ready", issue=1, outcome="waiting")
    for index in range(3):
        store.record_flow("tick", index=index)
    assert len([row for row in store.flow_records() if row["issue"] == 1]) == 1


def test_phase_readers_keep_same_time_places_and_orders_but_replace_checkpoints(store):
    start = store.clock[0]
    for phase, fields in [("batch-ci", {"issue": 1}), ("gate-tick-gap", {"issue": 1}),
                          ("order-preparation", {"order": "a"}), ("order-preparation", {"order": "b"})]:
        store.record_phase(phase, start.isoformat(), at=start + timedelta(minutes=10), **fields)
        store.record_phase(phase, start.isoformat(), at=start + timedelta(minutes=20), **fields)
    end = start + timedelta(minutes=20)
    intervals = orch_scorecard_records._phase_intervals(store, end)
    assert len(intervals) == 4
    assert all(span == (start, end) for _, _, span in intervals)
    assert len(orch_scorecard._events(store, end, 1)) == 4


def test_order_preparation_survives_retries_and_closes_at_enqueue(store):
    start = store.clock[0]
    order_prepare.path_phase(store, "a", "order-preparation", publication="returned")
    store.clock[0] += timedelta(minutes=20)
    order_prepare.path_phase(store, "a", "order-preparation")
    store.clock[0] += timedelta(minutes=20)
    order_prepare.path_phase(store, "a", "order-publication", publication="new", replace=True)
    # Completion of an older request must not close the replacement.
    order_prepare.path_phase(store, "a", None, publication="returned")
    store.clock[0] += timedelta(minutes=30)
    order_prepare.path_phase(store, "a", None, publication="new")
    spans = orch_scorecard_records._phase_intervals(store, store.clock[0])
    assert sorted((phase, (b - a).total_seconds() / 60) for _, phase, (a, b) in spans) == [
        ("order-preparation", 40), ("order-publication", 30)]
    assert all(event.get("order") == "a" for event in orch_events.events(store, "phase"))
    assert min(a for _, _, (a, _) in spans) == start


def test_phase_metadata_failure_is_visible_and_does_not_stop_the_order(store, monkeypatch, caplog):
    monkeypatch.setattr(store, "_write_json", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    order_prepare.path_phase(store, "a", "order-preparation")
    assert "Phase record write failed" in caplog.text


def test_tick_gap_uses_the_previous_heartbeat_and_excludes_running_work(store):
    start = store.clock[0]
    store.update_local_issue(1, state="open", labels=["approved"])
    store.update_local_issue(2, state="open", labels=["approved"])
    store.start_run(2, branch="issue-2", worktree=store.worktree_path(2))
    store.update_run(2, phase="implementing", phase_started_at=start.isoformat())
    store.write_heartbeat("gate", start)
    ctx = SimpleNamespace(store=store, now=store._now)
    store.clock[0] += timedelta(minutes=5, seconds=1)
    scheduler._record_tick_gap(ctx)
    assert not orch_events.events(store, "phase")
    store.clock[0] = start + timedelta(minutes=40)
    scheduler._record_tick_gap(ctx)
    events = orch_events.events(store, "phase")
    assert [(event["issue"], event["phase"], event["seconds"]) for event in events] == [(1, "gate-tick-gap", 2400)]


def test_tick_gap_cannot_start_before_a_worker_enters_its_wait(store, monkeypatch):
    from foragent import merge_owner

    start = store.clock[0]
    store.write_heartbeat("gate", start)
    store.update_local_issue(1, state="open", labels=["approved"])
    entered = start + timedelta(minutes=20)
    monkeypatch.setattr(merge_owner, "admissions", lambda store: {(1, "run"): {"at": entered.isoformat()}})
    store.clock[0] += timedelta(minutes=40)
    scheduler._record_tick_gap(SimpleNamespace(store=store, now=store._now))
    event, = orch_events.events(store, "phase")
    assert event["phase_started_at"] == entered.isoformat()
    assert event["seconds"] == 1200


def test_tick_gap_does_not_invent_waiting_before_the_issue_was_observed(store):
    store.write_heartbeat("gate", store.clock[0])
    store.clock[0] += timedelta(minutes=40)
    store.update_local_issue(1, state="open", labels=["approved"])
    scheduler._record_tick_gap(SimpleNamespace(store=store, now=store._now))
    assert not orch_events.events(store, "phase")


def test_owner_intervals_close_at_train_check_and_merge_boundaries(store):
    from foragent import merge_owner
    from foragent.run_merge_turn import Seat

    start = store.clock[0]
    ctx = SimpleNamespace(store=store, now=store._now)
    place = Seat(1, start.isoformat(), "main")
    job = {"phase": "waiting", "path_phase": "merge-owner-queue", "path_phase_started_at": start.isoformat()}
    merge_owner._save(ctx, place, job)
    store.clock[0] += timedelta(minutes=5)
    merge_owner._set_phase(ctx, place, job, "batch-ci")
    store.clock[0] += timedelta(minutes=10)
    merge_owner._set_phase(ctx, place, job, "merge-owner-checks")
    store.clock[0] += timedelta(minutes=7)
    job.update(phase="merged", merge_fact={"merged_at": store.clock[0].isoformat()})
    merge_owner._save(ctx, place, job)
    spans = orch_scorecard_records._phase_intervals(store, store.clock[0])
    assert [(phase, (b - a).total_seconds() / 60) for _, phase, (a, b) in spans] == [
        ("merge-owner-queue", 5), ("batch-ci", 10), ("merge-owner-checks", 7)]
    store.clock[0] += timedelta(minutes=10)
    merge_owner._save(ctx, place, job)
    assert orch_scorecard_records._phase_intervals(store, store.clock[0]) == spans


def test_2000_individual_observations_do_not_replay_20000_archived_rows(store, monkeypatch):
    # Populate through the real rotation boundary, without making setup itself
    # perform 20,000 appends and repeated generation reads.
    store.root.mkdir(parents=True)
    for generation in range(20):
        rows = [{"at": store.clock[0].isoformat(), "kind": "issue-state", "issue": n,
                 "run_started_at": None, "data": {"state": "open", "labels": ["approved"], "blocked_by": []}}
                for n in range(1, 1001)]
        (store.root / state_logs.FLOW_OLD_FILE).write_text("".join(json.dumps(row) + "\n" for row in rows))
        store._retain_flow_generation()
    store.record_flow("tick", status="ok")
    restarted = StateStore(store.root, now=store._now)
    read, replayed = restarted._flow_generation, []

    def measured(name):
        if isinstance(name, type(store.root)) and name.parent.name == state_logs.FLOW_RETAINED:
            replayed.append(name)
        return read(name)

    monkeypatch.setattr(restarted, "_flow_generation", measured)
    # Half are unchanged archived issues; half have never been observed. Both
    # lookups must be cheap, including a fresh StateStore/process cache.
    for number in range(1, 2001):
        snapshot = {"issues": {str(number): {"number": number, "state": "open", "labels": ["approved"],
                                            "observed_at": store.clock[0].isoformat()}}}
        restarted._record_issue_states(snapshot, {}, store.clock[0], {number}, edges={number: []})
    assert replayed == [], "Latest-state comparison replayed lifetime archive files"
    assert len([row for row in restarted.flow_records() if row["kind"] == "issue-state"]) == 21000


@pytest.mark.parametrize("window", ["since", "until", "both"])
def test_time_windows_skip_archives_outside_their_recorded_range(store, monkeypatch, window):
    monkeypatch.setattr(state_logs, "FLOW_GENERATION_LINES", 2)
    start = store.clock[0]
    for day in range(8):
        store.clock[0] = start + timedelta(days=day)
        store.record_flow("activity", issue=1, day=day)
    rows = store.flow_records()
    bounds = {"since": start + timedelta(days=5)} if window == "since" else {
        "until": start + timedelta(days=2)} if window == "until" else {
        "since": start + timedelta(days=2), "until": start + timedelta(days=4)}
    expected = [row for row in rows if ("since" not in bounds or datetime.fromisoformat(row["at"]) >= bounds["since"])
                and ("until" not in bounds or datetime.fromisoformat(row["at"]) < bounds["until"])]
    read = store._flow_generation

    def measured(name):
        if isinstance(name, type(store.root)):
            contents = [json.loads(line) for line in (store.root / name).read_text().splitlines()]
            assert any(row in expected for row in contents), "Deserialized an archive outside the requested window"
        return read(name)

    monkeypatch.setattr(store, "_flow_generation", measured)
    assert store.flow_records(**bounds) == expected


def test_archive_expiry_requires_completion_and_preserves_reopened_or_unknown_issues(store, monkeypatch):
    monkeypatch.setattr(state_logs, "FLOW_GENERATION_LINES", 2)
    start = store.clock[0]
    for number in (1, 2, 3, 4, 5):
        store.update_local_issue(number, state="open", labels=["approved"] if number == 1 else [])
    # Closed locally, merged by the independent owner, and merged by a worker.
    store.update_local_issue(3, state="closed")
    for number in (4, 5):
        run = store.start_run(number, branch=f"issue-{number}", worktree=store.worktree_path(number))
        store.finish_run(number, outcome="merge-queued" if number == 4 else "merged")
        if number == 4:
            store.record_owner_merge(number, run.started_at, {"merged_at": start.isoformat()})
    for number in range(1, 7):  # #6 has history but no completion evidence.
        store.record_flow("approval-history", issue=number)
    for _ in range(4):
        store.record_flow("tick")
    store.clock[0] += state_logs.JOURNAL_RETENTION + timedelta(days=1)
    store.update_local_issue(1, state="open", labels=["approved"])  # Unchanged observations write no new flow row.
    store.queue_github_write(2, "close_issue", reason="completed")
    assert next(row for row in store.local_issues() if row["number"] == 2)["state"] == "closed"
    store.update_local_issue(5, state="open", merged=False)  # A later open observation supersedes its old merge.
    store.trim_journals(store.clock[0])
    history = {row["issue"] for row in store.flow_records() if row["kind"] == "approval-history"}
    assert history == {1, 2, 5, 6}
    # Finishing a long wait starts a fresh retention period, rather than
    # immediately expiring its older, unchanged approval history.
    def lost_closing_record(*args, **kwargs):
        raise OSError("closing flow record unavailable")

    with monkeypatch.context() as failed_flow:
        failed_flow.setattr(store, "_write_flow", lost_closing_record)
        store.update_local_issue(1, state="closed")
    store.trim_journals(store.clock[0])
    assert any(row["issue"] == 1 and row["kind"] == "approval-history" for row in store.flow_records())
    store.clock[0] += state_logs.JOURNAL_RETENTION + timedelta(days=1)
    store.trim_journals(store.clock[0])
    assert not any(row["issue"] == 1 and row["kind"] == "approval-history" for row in store.flow_records())


def test_tick_gap_includes_the_refresh_after_the_previous_heartbeat(store):
    start = store.clock[0]
    store.write_heartbeat("gate", start)
    store.clock[0] += timedelta(seconds=1)
    store.update_local_issue(1, state="open", labels=["approved"])
    store.clock[0] = start + timedelta(minutes=40)
    scheduler._record_tick_gap(SimpleNamespace(store=store, now=store._now))
    event, = orch_events.events(store, "phase")
    assert event["phase_started_at"] == (start + timedelta(seconds=1)).isoformat()
    assert event["seconds"] == 2399


@pytest.mark.parametrize("callback", [None, "order-preparation", "order-publication"])
def test_superseded_publication_callbacks_cannot_move_the_replacement_interval(store, callback):
    order_prepare.path_phase(store, "a", "order-publication", publication="old", replace=True)
    store.clock[0] += timedelta(minutes=10)
    order_prepare.path_phase(store, "a", "order-publication", publication="replacement", replace=True)
    path = store.root / "order-phases" / f"{order_prepare._slug('a')}.json"
    before, events = path.read_bytes(), orch_events.events(store, "phase")
    store.clock[0] += timedelta(minutes=20)
    order_prepare.path_phase(store, "a", callback, publication="old")
    assert path.read_bytes() == before
    assert orch_events.events(store, "phase") == events


def test_an_out_of_order_phase_checkpoint_cannot_shorten_an_interval(store):
    start = store.clock[0]
    order_prepare.path_phase(store, "a", "order-preparation")
    store.clock[0] += timedelta(minutes=20)
    order_prepare.path_phase(store, "a", "order-publication", publication="new", replace=True)
    store.clock[0] = start + timedelta(minutes=10)
    order_prepare.path_phase(store, "a", None, publication="new")
    store.clock[0] = start + timedelta(minutes=40)
    order_prepare.path_phase(store, "a", None, publication="new")
    assert [(phase, (end - begin).total_seconds()) for _, phase, (begin, end) in
            orch_scorecard_records._phase_intervals(store, store.clock[0])] == [
                ("order-preparation", 1200), ("order-publication", 1200)]


@pytest.mark.parametrize("failure", [OSError("disk unavailable"), ValueError("bad phase metadata"), RuntimeError("timeout")])
def test_phase_accounting_failures_do_not_stop_run_or_owner_transitions(store, monkeypatch, caplog, failure):
    from foragent import merge_owner
    from foragent.run_merge_turn import Seat

    run = store.start_run(1, branch="issue-1", worktree=store.worktree_path(1))
    store.update_run(1, phase="implementing", phase_started_at=store.clock[0].isoformat())

    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(orch_events, "append_event", broken)
    store.record_phase("batch-ci", "malformed timestamp", issue=1)
    monkeypatch.setattr("foragent.state_phases._close_phase", broken)
    store.update_run(1, phase="reviewing", phase_started_at=store.clock[0].isoformat())
    assert store.get_run(1).phase == "reviewing"
    monkeypatch.setattr(store, "record_phase", broken)
    ctx = SimpleNamespace(store=store, now=store._now)
    place = Seat(1, run.started_at, "main")
    job = {"phase": "waiting", "path_phase": "batch-ci", "path_phase_started_at": "malformed timestamp"}
    merge_owner._set_phase(ctx, place, job, "merge-owner-checks")
    job.update(phase="merged", merge_fact={"merged_at": "malformed timestamp"})
    merge_owner._save(ctx, place, job)
    assert json.loads(merge_owner._path(ctx, 1).read_text())["phase"] == "merged"
    assert "phase" in caplog.text.lower() and "failed" in caplog.text.lower()


@pytest.mark.parametrize("phase", ["merge-owner-queue", "batch-ci"])
def test_every_queued_member_gets_one_cause_even_when_phase_checkpoint_fails(store, monkeypatch, caplog, phase):
    from foragent import merge_owner
    from foragent.run_merge_turn import Seat

    start = store.clock[0].isoformat()
    row = {"n": 1, "started_at": start, "target": "main"}
    monkeypatch.setattr(merge_owner, "_admitted", lambda store: {(1, start): row})
    job = {"phase": "waiting", "path_phase": phase, "path_phase_started_at": start}
    ctx = SimpleNamespace(store=store, now=store._now, merge_owner_wait_cause="waiting for merge train CI")
    merge_owner._save(ctx, Seat(1, start, "main"), job)

    def broken(*args, **kwargs):
        raise ValueError("unreadable phase")

    monkeypatch.setattr(store, "record_phase", broken)
    before = merge_owner._path(ctx, 1).read_bytes()
    merge_owner._report_waiting(ctx)
    records = [row for row in store.flow_records() if row["kind"] == "merge-owner-wait"]
    assert [(row["issue"], row["data"]["reason"]) for row in records] == [(1, "waiting for merge train CI")]
    assert merge_owner._path(ctx, 1).read_bytes() == before  # A checkpoint never rewrites the owner's job.
    assert "phase" in caplog.text.lower() and "failed" in caplog.text.lower()


def test_landing_pending_ci_recheck_restores_every_members_wait_phase(batched, monkeypatch):
    from foragent import ci_mode, merge_owner_batch

    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    owner_support.gate(case)
    calls = []

    def outcome(proof, ctx):
        calls.append(ctx.now())
        if len(calls) == 1:  # Preparation saw green; landing's final recheck sees pending.
            return "pass"
        raise ci_mode.MissingComparison("train tip CI pending at final recheck")

    monkeypatch.setattr(merge_owner_batch.Evidence, "outcome", outcome)
    case.clock[0] += timedelta(minutes=5)
    owner_support.gate(case)
    assert len(calls) >= 2
    assert not case.target_pushes
    for number in (5, 1):
        job = json.loads((case.store.root / "merge-owner" / f"{number}.json").read_text())
        assert job["path_phase"] == "batch-ci"
    # A half-hour pause after deferral must accrue to CI waiting, not machine checks.
    paused = case.clock[0]
    case.clock[0] += timedelta(minutes=30)
    owner_support.gate(case)
    for number in (5, 1):
        spans = orch_scorecard_records._phase_intervals(case.store, case.clock[0])
        assert any(n == number and phase == "batch-ci" and start <= paused and end >= paused + timedelta(minutes=30)
                   for n, phase, (start, end) in spans)
        assert not any(n == number and phase == "merge-owner-checks" and end - start >= timedelta(minutes=30)
                       for n, phase, (start, end) in spans)


@pytest.mark.parametrize("writer", ["flow", "change", "phase", "order"])
def test_observational_lock_timeouts_do_not_poison_the_callers_next_lock(store, monkeypatch, caplog, writer):
    from foragent.file_locks import reporting
    from foragent.state_locks import _file_lock

    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "0")
    lock = store.root / ("flow.lock" if writer in {"flow", "change"} else "state.lock")
    with reporting(store.root):
        with _file_lock(lock):
            if writer == "flow":
                store.record_flow("ready", issue=1)
            elif writer == "change":
                store.record_flow_change("ready", issue=1)
            elif writer == "phase":
                store.record_phase("batch-ci", store.clock[0].isoformat(), issue=1)
            else:
                order_prepare.path_phase(store, "a", "order-preparation")
        with store._state_lock():
            store._write_json(store.root / "continued.json", {"continued": True})
    assert json.loads((store.root / "continued.json").read_text()) == {"continued": True}
    assert writer in caplog.text.lower() or "phase" in caplog.text.lower()
    assert "failed" in caplog.text.lower()


def test_ready_cause_checkpoints_use_latest_facts_and_release_archived_waits(store, monkeypatch):
    from foragent import scheduler_flow

    monkeypatch.setattr(state_logs, "FLOW_GENERATION_LINES", 2)
    store.record_flow("ready", issue=1, reason="starts-off", outcome="waiting")
    for _ in range(6):
        store.record_flow("tick")

    def no_replay(**kwargs):
        pytest.fail("Gate ready causes replayed lifetime history")

    read = store.flow_records
    monkeypatch.setattr(store, "flow_records", no_replay)
    scheduler_flow._flush(store, scheduler_flow._Tick(complete=True))
    assert [row["data"]["reason"] for row in read() if row["kind"] == "ready"] == ["starts-off", "released"]


@pytest.mark.parametrize("deferred", ["stop", "github"])
def test_landing_other_deferrals_leave_checks_and_restore_queue_wait(batched, monkeypatch, deferred):
    from foragent import merge_owner_batch
    from foragent.run_attempt import ExecutorStopped
    from foragent.tracker import TrackerError

    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    owner_support.gate(case)
    calls = []

    def outcome(proof, ctx):
        calls.append(ctx.now())
        if len(calls) == 1:
            return "pass"
        raise ExecutorStopped("stopped", "project stop") if deferred == "stop" else TrackerError(
            "HTTP 502 Bad Gateway", transport=True)

    monkeypatch.setattr(merge_owner_batch.Evidence, "outcome", outcome)
    case.clock[0] += timedelta(minutes=5)
    owner_support.gate(case)
    assert len(calls) >= 2 and not case.target_pushes
    for number in (5, 1):
        job = json.loads((case.store.root / "merge-owner" / f"{number}.json").read_text())
        assert job["path_phase"] == "merge-owner-queue"


def test_batch_retirement_history_failure_does_not_change_the_merge(batched, monkeypatch, caplog):
    from foragent import merge_owner
    from foragent.file_locks import LockTimeout
    from helpers.merge_batch import green

    case = batched
    case.pending_batch = True
    queue_workers(case, [5, 1])
    owner_support.gate(case)
    green(case)

    def unavailable(self, **kwargs):
        raise LockTimeout("batch history unavailable")

    # gate() deliberately constructs a fresh store, as a real owner process does.
    monkeypatch.setattr(StateStore, "flow_records", unavailable)
    owner_support.gate(case)
    assert len(case.target_pushes) == 1
    assert {n for n, _ in case.store.owner_merge_facts()} == {5, 1}
    assert merge_owner.admissions(case.store) == {}
    assert "flow" in caplog.text.lower() and "failed" in caplog.text.lower()


@pytest.mark.parametrize("failed_read", ["ci", "phase"])
def test_batch_retirement_finishes_despite_observational_lookup_failures(store, monkeypatch, caplog, failed_read):
    from foragent import merge_owner, merge_owner_batch
    from foragent.tracker import TrackerError

    def broken(*args, **kwargs):
        # A lookup that fails without a GitHub wait; a wait must reach the owner's pause instead
        # (test_batch_github_wait_reaches_owner_pause_without_retirement).
        error = TrackerError("HTTP 404 Not Found") if failed_read == "ci" else ValueError("bad job metadata")
        assert not getattr(error, "waits", False)
        raise error

    ctx = SimpleNamespace(store=store, now=store._now, tracker=SimpleNamespace(
        pr_acceptance_reports=broken if failed_read == "ci" else lambda head: []))
    monkeypatch.setattr(merge_owner, "admissions", broken if failed_read == "phase" else lambda store: {})
    retired = []

    def retire(*args, **kwargs):
        retired.append(kwargs["reason"])
        return kwargs["reason"]

    monkeypatch.setattr(merge_owner_batch, "_record_retirement", retire)
    # candidate == base leaves no branch to clean up, isolating the real
    # retirement and accounting boundaries from Git transport.
    merge_owner_batch._close(ctx, SimpleNamespace(actions=[]), "base",
                             [(1, "head", "base", store.clock[0].isoformat())], "finished", used=0)
    assert retired == ["finished"]
    assert any(row["kind"] == "merge-batch" for row in store.flow_records())
    assert "failed" in caplog.text.lower()
    assert ("flow" if failed_read == "ci" else "phase") in caplog.text.lower()


def test_retired_train_leaves_checks_even_when_remote_cleanup_defers(store, monkeypatch):
    from foragent import merge_owner, merge_owner_batch
    from foragent.tracker import TrackerError

    started = store.clock[0].isoformat()
    row = {"n": 1, "started_at": started, "target": "main"}
    job = {"phase": "waiting", "path_phase": "merge-owner-checks", "path_phase_started_at": started}
    monkeypatch.setattr(merge_owner, "admissions", lambda store: {(1, started): row})
    monkeypatch.setattr(merge_owner, "_load", lambda *args: job)
    monkeypatch.setattr(merge_owner_batch, "_record_retirement", lambda *args, **kwargs: kwargs["reason"])

    def unavailable(branch):
        raise TrackerError("HTTP 502 Bad Gateway", transport=True)

    ctx = SimpleNamespace(store=store, now=store._now, tracker=SimpleNamespace(pr_acceptance_reports=lambda head: []),
                          git=SimpleNamespace(remote_branch_sha=unavailable))
    with pytest.raises(TrackerError):
        merge_owner_batch._close(ctx, SimpleNamespace(actions=[]), "base", [(1, "head", "tip", started)],
                                 "retired", used=0)
    assert job["path_phase"] == "merge-owner-queue"
    assert json.loads((store.root / "merge-owner" / "1.json").read_text())["path_phase"] == "merge-owner-queue"


def test_observing_passes_a_github_wait_to_the_caller_and_logs_any_other_failure(caplog):
    from foragent.tracker import TrackerError

    wait = TrackerError("HTTP 429 API rate limit exceeded")
    assert wait.waits, "Fixture: a rate limit is a GitHub wait"
    with pytest.raises(TrackerError):
        with state_logs.observing("Flow", writer="batch CI duration"):
            raise wait
    assert not [record for record in caplog.records if "record write failed" in record.getMessage()]

    other = TrackerError("HTTP 404 Not Found")
    assert not other.waits, "Fixture: a missing resource is not a wait"
    with state_logs.observing("Flow", writer="batch CI duration"):
        raise other
    with state_logs.observing("Phase", issue=8):
        raise OSError("disk full")
    failed = [record.getMessage() for record in caplog.records if "record write failed" in record.getMessage()]
    assert len(failed) == 2 and failed[0].startswith("Flow") and failed[1].startswith("Phase"), failed
