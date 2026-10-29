"""Transient failures around the reconciled event (#130)."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "acceptance"))


def _case(git_history, tmp_path, monkeypatch):
    from test_orch_review_handoff import make_case, tick_context

    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    case = make_case(git_history, tmp_path, monkeypatch)
    case.tracker.record_label(1, "approved", "human")
    case.store.update_run(1, pid=88001, phase="implementing")
    (case.store.root / "runs/1.json").unlink()
    return case, tick_context(case, start_runs=False)


def _reconciled(store):
    path = store.root / "orch-events.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    return [event for event in map(json.loads, lines) if event["kind"] == "reconciled"]


def _tick_failing_once(ctx, monkeypatch, target, name, predicate=lambda *args, **kwargs: True):
    from foragent import scheduler

    original = getattr(target, name)
    calls = []

    def flaky(*args, **kwargs):
        if not calls and predicate(*args, **kwargs):
            calls.append(1)
            raise OSError("transient")
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, flaky)
    with pytest.raises(OSError):
        scheduler.tick(ctx)
    assert calls == [1]
    monkeypatch.setattr(target, name, original)


def test_failed_event_write_is_retried_by_the_next_tick(git_history, tmp_path, monkeypatch):
    from foragent import scheduler

    case, ctx = _case(git_history, tmp_path, monkeypatch)
    _tick_failing_once(ctx, monkeypatch, scheduler, "append_event",
                       lambda store, at, issue, kind, *args, **kwargs: kind == "reconciled")
    assert _reconciled(case.store) == []
    scheduler.tick(ctx)
    assert case.tracker.get_issue(1).labels == {"approved", "ready"}
    assert [event["issue"] for event in _reconciled(case.store)] == [1]


def test_failed_record_after_event_does_not_repeat_the_event(git_history, tmp_path, monkeypatch):
    from foragent import scheduler

    case, ctx = _case(git_history, tmp_path, monkeypatch)
    _tick_failing_once(ctx, monkeypatch, type(case.store), "start_run")
    assert len(_reconciled(case.store)) == 1
    scheduler.tick(ctx)
    scheduler.tick(ctx)
    assert case.tracker.get_issue(1).labels == {"approved", "ready"}
    assert len(_reconciled(case.store)) == 1
