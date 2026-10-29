"""Boundary rules of StateStore.prune_logs / trim_journals and the tick housekeeping."""

from datetime import UTC, datetime, timedelta
import json
import os
import threading
from types import SimpleNamespace

from foragent import scheduler_housekeeping, state
from foragent.state import StateStore

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def make(tmp_path):
    clock = [NOW]
    return StateStore(tmp_path / "state", now=lambda: clock[0]), clock


def run(store, clock, number, *, days, outcome="merged", finish=True):
    current = clock[0]
    clock[0] = NOW - timedelta(days=days)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    if finish:
        store.finish_run(number, outcome=outcome)
    clock[0] = current


def write(store, name, *, days=0, size=10):
    path = store.root / "logs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_companions_and_orphans_follow_their_issue_and_other_files_are_ignored(tmp_path):
    store, clock = make(tmp_path)
    run(store, clock, 1, days=8)
    run(store, clock, 2, days=8)
    expired = [write(store, name) for name in ("issue-1.log", "issue-1.stdout", "issue-1-review-1-1.log")]
    orphan = write(store, "issue-2-review-1-1.log")  # issue-2.log itself is missing.
    other = [write(store, name, days=90) for name in ("issue-12.log", "notes.txt", "issue-x.log")]
    assert set(store.prune_logs(NOW)) == {*expired, orphan}
    assert all(path.is_file() for path in other), "issue-12 has no finished run; others are not issue files"


def test_active_runs_and_runs_without_a_finish_are_kept_by_age(tmp_path):
    store, clock = make(tmp_path)
    run(store, clock, 1, days=40, outcome="blocked")
    run(store, clock, 1, days=1, finish=False)  # A newer run of issue 1 is active now.
    run(store, clock, 2, days=40, finish=False)
    kept = [write(store, "issue-1.log", days=40), write(store, "issue-2.log", days=40),
            write(store, "issue-3.log", days=400)]
    assert store.prune_logs(NOW) == []
    assert all(path.is_file() for path in kept)


def test_newest_finished_run_decides_and_mtime_is_the_fallback_age(tmp_path):
    store, clock = make(tmp_path)
    run(store, clock, 1, days=40, outcome="blocked")
    run(store, clock, 1, days=3, outcome="merged")
    young = write(store, "issue-1.log", days=40)
    history = store.root / "runs.jsonl"
    run(store, clock, 2, days=0, outcome="merged")
    lines = history.read_text().splitlines()
    value = json.loads(lines[-1])
    del value["finished_at"]  # A record written before finish times were saved.
    history.write_text("\n".join([*lines[:-1], json.dumps(value)]) + "\n")
    old, fresh = write(store, "issue-2.log", days=8), write(store, "issue-2.stdout", days=6)
    assert store.prune_logs(NOW) == [old]
    assert young.is_file() and fresh.is_file()


def test_size_rule_deletes_oldest_whole_issues_never_active_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "LOG_LIMIT_BYTES", 100)
    store, clock = make(tmp_path)
    run(store, clock, 1, days=1, finish=False)  # Active and oldest by mtime.
    active = write(store, "issue-1.log", days=20, size=40)
    no_finish = [write(store, "issue-2.log", days=10, size=30), write(store, "issue-2.stdout", days=9, size=10)]
    run(store, clock, 3, days=2)
    newest = write(store, "issue-3.log", days=2, size=30)
    write(store, "gate.out", size=10)  # Counted, never deleted.
    assert set(store.prune_logs(NOW)) == set(no_finish)
    assert active.is_file() and newest.is_file() and store.logs_total() == 80


def test_a_held_worktree_lock_skips_the_issue_this_tick(tmp_path):
    store, clock = make(tmp_path)
    run(store, clock, 1, days=8)
    path = write(store, "issue-1.log")
    with store.worktree_lock(1):
        assert store.prune_logs(NOW) == [] and path.is_file()
    assert store.prune_logs(NOW) == [path]


def test_start_run_holds_the_worktree_lock_while_creating_the_record(tmp_path, monkeypatch):
    store, clock = make(tmp_path)
    seen = []
    write_run = store._write_run

    def check(*args):
        result = []
        thread = threading.Thread(target=lambda: result.append(
            store.try_worktree_lock(1).__enter__()))
        thread.start()
        thread.join()
        seen.append(result[0])
        return write_run(*args)

    monkeypatch.setattr(store, "_write_run", check)
    run(store, clock, 1, days=0, finish=False)
    assert seen == [False]


def test_trim_keeps_unparseable_lines_and_reports_only_changed_journals(tmp_path):
    store, _ = make(tmp_path)
    store.root.mkdir(parents=True)
    old = json.dumps({"at": (NOW - timedelta(days=31)).isoformat(), "kind": "x"})
    keep = ["not json", json.dumps({"kind": "no at"}), json.dumps({"at": "yesterday"}),
            json.dumps({"at": (NOW - timedelta(days=29)).isoformat()})]
    orch = store.root / "orch-events.jsonl"
    orch.write_text("\n".join([old, *keep]) + "\n")
    assert store.trim_journals(NOW) == [orch]
    assert orch.read_text().splitlines() == keep
    assert store.trim_journals(NOW) == [] and not (store.root / "gate-events.jsonl").exists()


def test_housekeeping_reports_trims_and_errors_and_checks_tidy_only_when_finished(tmp_path, monkeypatch):
    store, _ = make(tmp_path)
    store.root.mkdir(parents=True)
    (store.root / "gate-events.jsonl").write_text(
        json.dumps({"at": (NOW - timedelta(days=40)).isoformat()}) + "\n")
    tidy_calls = []
    monkeypatch.setattr(scheduler_housekeeping.tidy, "post_if_due", lambda s: tidy_calls.append(s))
    ctx = SimpleNamespace(store=store, now=lambda: NOW)
    report = SimpleNamespace(actions=[])
    with scheduler_housekeeping.after_tick(ctx) as done:
        done(report)
    assert report.actions == ["Trimmed gate-events.jsonl lines older than 30 days"] and tidy_calls == []
    monkeypatch.setattr(state, "LOG_LIMIT_BYTES", 5)
    run(store, [NOW], 1, days=0, finish=False)
    write(store, "issue-1.log", size=10)
    report = SimpleNamespace(actions=[])
    with scheduler_housekeeping.after_tick(ctx) as done:
        done(report, finished=True)
    assert report.actions == ["Logs still above 500 MB after pruning; active runs' logs are kept"]
    assert tidy_calls == [store]

    def broken(now):
        raise PermissionError("denied")

    monkeypatch.setattr(store, "prune_logs", broken)
    report = SimpleNamespace(actions=[])
    with scheduler_housekeeping.after_tick(ctx) as done:
        done(report)
    assert report.actions == ["Log pruning failed: denied"]


def finish_before_lock(store, monkeypatch, number, outcome):
    """Finish issue ``number``'s active run after pruning read history, just before it locks."""
    take = store.try_worktree_lock

    def late_finish(n):
        if n == number and store.get_run(n).status == "running":
            store.finish_run(n, outcome=outcome)
        return take(n)

    monkeypatch.setattr(store, "try_worktree_lock", late_finish)


def test_a_run_finishing_after_history_was_read_keeps_its_fresh_logs(tmp_path, monkeypatch):
    store, clock = make(tmp_path)
    run(store, clock, 1, days=8)  # Expired merged run...
    run(store, clock, 1, days=0, finish=False)  # ...then a new run writes fresh logs.
    fresh = [write(store, "issue-1.log"), write(store, "issue-1.stdout")]
    finish_before_lock(store, monkeypatch, 1, "failed")
    assert store.prune_logs(NOW) == []
    assert all(path.is_file() for path in fresh)
    assert store.prune_logs(NOW) == [], "the failed run now keeps them 30 days"


def test_size_order_skips_an_issue_whose_run_finished_after_ordering(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "LOG_LIMIT_BYTES", 50)
    store, clock = make(tmp_path)
    run(store, clock, 1, days=6)
    run(store, clock, 1, days=0, finish=False)
    newest = write(store, "issue-1.log", days=0, size=40)
    run(store, clock, 2, days=3)
    older = write(store, "issue-2.log", days=3, size=40)
    finish_before_lock(store, monkeypatch, 1, "merged")
    assert store.prune_logs(NOW) == [older]
    assert newest.is_file()
