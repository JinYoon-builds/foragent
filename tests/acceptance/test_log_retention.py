"""Acceptance test for log-retention. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
import os

from test_heartbeats import heartbeats_offline, heartbeats_write


def log_retention_finished_log(store, clock, number, *, days, outcome, size=20):
    current = clock[0]
    clock[0] -= timedelta(days=days)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    store.finish_run(number, outcome=outcome)
    path = store.log_path(number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (clock[0].timestamp(), clock[0].timestamp()))
    clock[0] = current
    return path


def test_log_retention_merged_logs_expire_after_seven_days(tmp_path):
    from conftest import BASE_TIME
    from foragent.state import StateStore

    clock = [BASE_TIME]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    old = log_retention_finished_log(store, clock, 1, days=8, outcome="merged")
    young = log_retention_finished_log(store, clock, 2, days=6, outcome="merged")
    removed = store.prune_logs(clock[0])
    assert removed == [old] and not old.exists() and young.is_file()
    assert store.prune_logs(clock[0]) == [], "pruning twice must be harmless"


def test_log_retention_blocked_and_failed_logs_keep_thirty_days(tmp_path):
    from conftest import BASE_TIME
    from foragent.state import StateStore

    clock = [BASE_TIME]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    old, young = [], []
    # retries-exceeded is the actual terminal outcome produced by run_issue.
    for number, outcome in enumerate(("blocked", "failed", "retries-exceeded"), 1):
        old.append(log_retention_finished_log(store, clock, number, days=31, outcome=outcome))
        young.append(log_retention_finished_log(store, clock, number + 10, days=29, outcome=outcome))
    assert set(store.prune_logs(clock[0])) == set(old)
    assert all(not path.exists() for path in old) and all(path.is_file() for path in young)


def test_log_retention_size_limit_removes_oldest_until_below_limit(tmp_path, monkeypatch):
    from conftest import BASE_TIME
    from foragent import state
    from foragent.state import StateStore

    # Shrink the production 500 MB LOG_LIMIT_BYTES cap instead of allocating 500 MB.
    assert state.LOG_LIMIT_BYTES == 500 * 1024 * 1024
    monkeypatch.setattr(state, "LOG_LIMIT_BYTES", 100)
    clock = [BASE_TIME]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    paths = [log_retention_finished_log(store, clock, number, days=days, outcome="merged", size=60)
             for number, days in ((1, 6), (2, 5), (3, 4))]
    assert set(store.prune_logs(clock[0])) == set(paths[:2])
    assert not paths[0].exists() and not paths[1].exists() and paths[2].is_file()
    assert sum(path.stat().st_size for path in (store.root / "logs").iterdir()) <= 100


def test_log_retention_trims_old_event_lines_and_preserves_heartbeat_files(tmp_path):
    from conftest import BASE_TIME
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    heartbeats = [heartbeats_write(store, name, BASE_TIME - timedelta(days=45)) for name in ("orch", "gate")]
    originals = {path: path.read_bytes() for path in heartbeats}
    for path in heartbeats:
        old = (BASE_TIME - timedelta(days=45)).timestamp()
        os.utime(path, (old, old))
    events = [{"at": (BASE_TIME - timedelta(days=days)).isoformat(), "issue": 1,
               "kind": "retry", "summary": f"event-{days}"} for days in (31, 29, 0)]
    journals = [store.root / name for name in ("orch-events.jsonl", "gate-events.jsonl")]
    for path in journals:
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
    store.prune_logs(BASE_TIME)
    for path in journals:
        assert [json.loads(line) for line in path.read_text().splitlines()] == events[1:]
    assert all(path.read_bytes() == originals[path] for path in heartbeats)


def test_log_retention_missing_or_empty_logs_are_safe_and_gate_prunes_once_per_tick(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick
    from test_orch_review_handoff import make_case, tick_context

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    case = make_case(git_history, tmp_path, monkeypatch)
    assert not (case.store.root / "logs").exists()
    assert case.store.prune_logs(case.clock[0]) == []
    (case.store.root / "logs").mkdir(exist_ok=True)
    assert case.store.prune_logs(case.clock[0]) == []
    calls = []
    prune = case.store.prune_logs

    def record(now):
        calls.append(now)
        return prune(now)

    monkeypatch.setattr(case.store, "prune_logs", record)
    ctx = tick_context(case, start_runs=False)
    for count in (1, 2):
        tick(ctx)
        assert calls == [case.clock[0]] * count
