"""Run reasons and recovery across process/GitHub boundaries."""

from datetime import timedelta
import json

import pytest

from foragent import runner
from foragent.state import StateStore
from foragent.tracker import TrackerError
from test_orch import review_case
from test_runner import run_case
from test_scheduler import NOW, approve, ctx


@pytest.mark.parametrize("entry", ["run", "review"])
def test_exception_reason_survives_reopening_and_reporting_failure(
    request, monkeypatch, entry,
):
    case = request.getfixturevalue("review_case" if entry == "review" else "run_case")
    message = "arbitrary upstream failure\r\n\t" + "한글 context " * 80

    def fail(*args, **kwargs):
        raise RuntimeError(message)

    def reporting_failed(*args, **kwargs):
        raise TrackerError("comment response lost")

    if entry == "review":
        monkeypatch.setattr(runner, "_review_only", fail)
    else:
        monkeypatch.setattr(runner, "_execute", fail)
    monkeypatch.setattr(case.tracker, "comment", reporting_failed)
    with pytest.raises(TrackerError, match="comment response lost"):
        (runner.review_only if entry == "review" else runner.run_issue)(1, case.ctx)
    persisted = StateStore(case.store.root)
    run = persisted.get_run(1)
    assert run.outcome == "error"
    assert run.detail == " ".join(message.split())[:500]
    assert persisted.read_history()[-1].detail == run.detail


@pytest.mark.parametrize("entry,expected", [
    ("run", "Runner failed:"), ("review", "Review runner failed:"),
])
def test_whitespace_exception_keeps_a_reason(request, monkeypatch, entry, expected):
    case = request.getfixturevalue("review_case" if entry == "review" else "run_case")

    def fail(*args, **kwargs):
        raise RuntimeError("\n\t ")

    monkeypatch.setattr(runner, "_review_only" if entry == "review" else "_execute", fail)
    assert (runner.review_only if entry == "review" else runner.run_issue)(1, case.ctx) == "error"
    persisted = StateStore(case.store.root)
    run = persisted.get_run(1)
    assert run.detail == expected
    assert persisted.read_history()[-1] == run


@pytest.mark.parametrize("operation", ["update_run", "finish_run"])
@pytest.mark.parametrize("detail,previous,expected", [
    ("new\n\t reason", "old reason", "new reason"),
    ("x" * 600, "old reason", "x" * 500),
    ("", "previous\n\t reason", "previous reason"),
    ("\n\t ", "previous\n\t reason", "previous reason"),
    ("\n\t ", "x" * 600, "x" * 500),
    ("", "", None),
    ("\n\t ", "", None),
    ("\n\t ", "\n\t ", None),
], ids=[
    "new-reason", "new-reason-truncated", "empty-keeps-previous", "whitespace-keeps-previous",
    "previous-truncated", "empty-default", "whitespace-default", "whitespace-previous-default",
])
def test_run_detail_normalizes_before_selecting_a_fallback(
    tmp_path, operation, detail, previous, expected,
):
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="issue-1", worktree=tmp_path / "worktree")
    # Older snapshots may contain details that have not been normalized.
    snapshot_path = store.root / "runs/1.json"
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["detail"] = previous
    snapshot_path.write_text(json.dumps(snapshot))

    if operation == "finish_run":
        run = store.finish_run(1, "error", detail=detail)
        expected = expected or "Run finished with outcome error."
    else:
        run = store.update_run(1, detail=detail)
        expected = expected or "Run detail unavailable."
    assert run.detail == expected
    persisted = StateStore(store.root)
    assert persisted.get_run(1) == run
    assert persisted.read_history() == ([run] if operation == "finish_run" else [])


@pytest.mark.parametrize("detail", ["", "\n\t "])
def test_merged_completion_does_not_require_a_reason(tmp_path, detail):
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="issue-1", worktree=tmp_path / "worktree")
    store.update_run(1, detail="previous reason")
    run = store.finish_run(1, "merged", detail=detail)
    assert run.detail == ""
    assert store.read_history() == [run]


@pytest.mark.parametrize("operation,label", [("remove_labels", "ready"), ("add_labels", "running")])
@pytest.mark.parametrize("applied", [False, True])
def test_start_failure_is_recoverable_after_reopening_without_a_crash(
    ctx, monkeypatch, operation, label, applied,
):
    from foragent.scheduler import tick

    approve(ctx)
    tick(ctx)
    original = getattr(ctx.tracker, operation)
    launches = []
    ctx.start_runs = True
    ctx.launcher = lambda number: launches.append(number) or 456

    def unavailable(number, labels, *args, **kwargs):
        if label in labels:
            if applied:
                original(number, labels, *args, **kwargs)
            raise TrackerError("connection disappeared without a response")
        return original(number, labels, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ctx.tracker, operation, unavailable)
        with pytest.raises(TrackerError):
            tick(ctx)
    assert launches == [] and ctx.store.read_history() == []
    # Even a fresh scheduler after the startup grace period knows no runner launched.
    ctx.now = lambda: NOW + timedelta(minutes=3)
    ctx.tracker._now = ctx.now
    ctx.store = StateStore(ctx.store.root, now=ctx.now)
    tick(ctx)
    tick(ctx)
    assert launches == [1]
    assert ctx.store.get_run(1).pid == 456
    assert ctx.store.read_history() == []
    assert ctx.store.load_flag(1) is None
    assert ctx.tracker.get_issue(1).labels == {"approved", "running"}


@pytest.mark.parametrize("outcome", ["held", "retries-exceeded", "limit-wait", "killed"])
def test_completion_keeps_the_recorded_reason_in_snapshot_and_history(tmp_path, outcome):
    store = StateStore(tmp_path / "state")
    store.start_run(1, branch="issue-1", worktree=tmp_path / "worktree")
    message = "actual reason\r\n\t" + "long output " * 100
    store.update_run(1, detail=message)
    finished = store.finish_run(1, outcome)
    assert finished.detail == " ".join(message.split())[:500]
    rows = [json.loads(line) for line in (store.root / "runs.jsonl").read_text().splitlines()]
    assert rows[-1]["detail"] == finished.detail


def test_publish_failure_delegates_to_the_shared_rate_limit_wait(run_case, monkeypatch):
    case = run_case
    error = TrackerError("upstream quota response")
    calls = []

    def fail(**kwargs):
        raise error

    def wait(number, context, exc):
        calls.append((number, context, exc))
        until = (case.now[0] + timedelta(minutes=10)).isoformat()
        context.store.update_run(number, waiting_until=until)
        context.tracker.add_labels(number, ["ready"])
        context.tracker.remove_labels(number, ["running"])
        return True

    monkeypatch.setattr(case.tracker, "create_pr", fail)
    monkeypatch.setattr(runner, "wait_for_rate_limit", wait)
    assert runner.run_issue(1, case.ctx) == "limit-wait"
    assert len(calls) == 1 and calls[0][0] == 1 and calls[0][2] is error
    assert case.store.get_run(1).waiting_until is not None
    assert case.store.get_run(1).detail == str(error)
    assert case.store.load_flag(1) is None
    assert case.store.get_run(1).attempts == 1
