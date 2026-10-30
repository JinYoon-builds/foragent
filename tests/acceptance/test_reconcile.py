"""Acceptance test for reconcile. Locked by Foragent; do not edit."""

import json

from test_heartbeats import heartbeats_offline


def reconcile_case(git_history, tmp_path, monkeypatch):
    from test_orch_review_handoff import make_case, tick_context

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    case = make_case(git_history, tmp_path, monkeypatch)
    case.tracker.record_label(1, "approved", "human")
    case.store.update_run(1, pid=88001, phase="implementing")
    return case, tick_context(case, start_runs=False)


def reconcile_events(store):
    path = store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_reconcile_orphan_running_label_returns_to_ready_with_visible_reconciled_event(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick
    from foragent.watch import recent_events

    case, ctx = reconcile_case(git_history, tmp_path, monkeypatch)
    (case.store.root / "runs/1.json").unlink()
    assert case.store.active_runs() == []
    tick(ctx)
    assert case.tracker.get_issue(1).labels == {"approved", "ready"}
    events = [event for event in recent_events(case.store) if event["kind"] == "reconciled"]
    assert len(events) == 1 and events[0]["issue"] == 1, reconcile_events(case.store)
    before = reconcile_events(case.store)
    tick(ctx)
    assert reconcile_events(case.store) == before, "the same repair must not be recorded twice"


def test_reconcile_dead_active_run_is_finished_and_requeued_exactly_once(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick

    case, ctx = reconcile_case(git_history, tmp_path, monkeypatch)

    def dead(pid, signal):
        assert pid == 88001 and signal == 0
        raise ProcessLookupError

    monkeypatch.setattr("foragent.scheduler.os.kill", dead)
    launches, stopped = [], []
    ctx.launcher = launches.append
    ctx.kill_runner = stopped.append
    tick(ctx)
    assert case.store.get_run(1).status == "finished"
    assert case.store.active_runs() == []
    assert case.tracker.get_issue(1).labels == {"approved", "ready"}
    history = case.store.read_history()
    events = reconcile_events(case.store)
    assert len(history) == 1 and events
    tick(ctx)
    ready = [event for event in case.tracker.label_events(1)
             if event.label == "ready" and event.action == "labeled"]
    assert len(ready) == 1 and stopped == [88001] and launches == []
    assert case.store.read_history() == history and reconcile_events(case.store) == events


def test_reconcile_consistent_live_records_are_unchanged_and_emit_no_event(git_history, tmp_path, monkeypatch):
    from foragent.scheduler import tick

    case, ctx = reconcile_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setattr("foragent.scheduler.os.kill", lambda pid, signal: None)
    try:  # #129's push journal is its own feature; this case pins reconcile events only.
        from foragent import orch_push
    except ImportError:
        orch_push = None
    if orch_push is not None and hasattr(orch_push, "after_tick"):
        monkeypatch.setattr(orch_push, "after_tick", lambda *args, **kwargs: ([], False))
    run = case.store.get_run(1)
    approval = case.store.load_approval(1)
    labels = case.tracker.get_issue(1).labels
    label_events = case.tracker.label_events(1)
    tick(ctx)
    tick(ctx)
    assert case.store.get_run(1) == run and case.store.load_approval(1) == approval
    assert case.tracker.get_issue(1).labels == labels
    assert case.tracker.label_events(1) == label_events
    assert reconcile_events(case.store) == [] and case.store.read_history() == []
