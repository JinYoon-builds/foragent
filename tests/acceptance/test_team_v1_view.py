"""Foreign work stays visible; conflict time counts intervals once per day."""
from datetime import timedelta
import json

from foragent import status
from foragent.orch_events import append_event
from test_orch_scorecard import scorecard_case
from test_orch_review_handoff import bind_cli
from team_v1_support import legacy, person


def test_real_conflict_timer_records_one_interval_and_excludes_waiting(
    git_history, tmp_path, monkeypatch,
):
    from types import SimpleNamespace
    from foragent.run_overlap import conflict_resolution
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))
    attempt = SimpleNamespace(ctx=case.ctx, n=1, parsed=SimpleNamespace(files=[]))
    with conflict_resolution(attempt, ["one.py", "two.py"]):
        case.clock[0] += timedelta(seconds=150)
        case.store.update_run(1, waited_seconds=30)
    path = case.store.root / "orch-events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    intervals = [e for e in events if e["kind"] == "conflict-resolution"]
    assert len(intervals) == 1, "The production timer must publish measured conflict work"
    assert intervals[0]["seconds"] == 120
    assert set(intervals[0]["paths"]) == {"one.py", "two.py"}
    assert intervals[0]["interval_id"] and intervals[0]["started_at"]


def test_status_preserves_foreign_rows_with_worked_by_fact(git_history, tmp_path, monkeypatch):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    legacy(case.ctx.config)
    person(case.tracker, monkeypatch, "alice")
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context
    tick(tick_context(case, start_runs=False))
    case.store.update_local_issue(2, title="Bob's task", body="", labels=["by:bob", "running"],
                                  state="open", blocked_by=[])
    payload = status.collect(case.store, case.ctx.config, case.clock[0])
    row = next(r for group in payload["issues"].values() for r in group if r["number"] == 2)
    assert row.get("orderer") == "bob" and row.get("worked_by") == "bob", row
    assert row.get("read_only") is True, row


def test_scorecard_reports_daily_conflict_minutes_without_duplicate_counting(
    git_history, tmp_path, monkeypatch, capsys,
):
    case = scorecard_case(git_history, tmp_path, monkeypatch)
    now = case.clock[0]
    # This event contract records one actual resolution interval, never one
    # duration per file (which would count a two-file conflict twice).
    event = {"interval_id": "issue-1/resolve-1", "seconds": 120,
             "paths": ["one.py", "two.py"], "started_at": (now - timedelta(minutes=3)).isoformat()}
    for _ in range(2):
        append_event(case.store, now - timedelta(minutes=1), 1, "conflict-resolution", "Resolved", **event)
    append_event(case.store, now - timedelta(days=1, minutes=1), 1, "conflict-resolution", "Older",
                 interval_id="issue-1/resolve-0", seconds=60, paths=["one.py"],
                 started_at=(now - timedelta(days=1, minutes=2)).isoformat())
    main = bind_cli(case, monkeypatch)
    assert main(["orch", "scorecard", "--json", "--days", "2"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["window"]["today"].get("conflict_resolution_minutes") == 2
    assert payload["window"]["previous"].get("conflict_resolution_minutes") == 1
