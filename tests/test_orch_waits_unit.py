"""Issue-owned waits deliver context, never approval or an independent work ledger."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch_inbox, orch_waits, order_waiting, scheduler
from foragent.config import Config
from foragent.state import StateStore
from foragent.tracker import FakeTracker, RecordingTracker, TrackerError

WAIT = ("Reason: Catalog review\nNext condition: Re-read #50 after closure; confirm the decision\n"
        "Responsible role: docs, orchestrator")


@pytest.fixture
def ctx(tmp_path):
    clock = [datetime(2026, 11, 1, tzinfo=UTC)]
    now = lambda: clock[0]
    context = SimpleNamespace(repo="owner/repo", config=Config(language="en"), now=now, clock=clock,
                              store=StateStore(tmp_path / "state", now=now))
    context.remote = FakeTracker(now=now)
    context.tracker = RecordingTracker(context.remote, context)
    context.tracker.add_issue(1, "Waiting work", "## Waiting\n" + WAIT)
    context.tracker.add_issue(50, "Catalog review")
    context.tracker.add_blocked_by(1, 50)
    return context


def scan(ctx, *, refresh=True):
    with ctx.store.tick_lock() as acquired:
        assert acquired
        if refresh:
            ctx.clock[0] += timedelta(minutes=6)
            ctx.tracker.refresh_issues()
        report = scheduler.TickReport()
        orch_waits.tick(ctx, report, confirmed=refresh)
    return report


@pytest.mark.parametrize("labels", [[], ["approved"]], ids=["unapproved", "approved"])
def test_wait_notice_preserves_context_and_never_changes_approval(ctx, labels):
    ctx.tracker.add_labels(1, labels)
    ctx.tracker.close_issue(50)
    before = ctx.remote.get_issue(1)
    assert scan(ctx).actions == []
    row, = orch_inbox.requests(ctx.store)
    assert row["from"] == "gate" and row["issue"] == 1
    assert row["waiting"] == WAIT and WAIT in row["text"]
    assert row["closed_blockers"] == [{"repository": "owner/repo", "number": 50}]
    assert "#1" in row["text"] and "owner/repo#50" in row["text"]
    assert ctx.remote.get_issue(1) == before
    assert ctx.store.load_approval(1) is None and ctx.store.active_runs() == []


@pytest.mark.parametrize("ineligible", ["one-open", "no-edges", "no-wait", "empty-wait", "closed-issue"])
def test_no_notice_without_an_open_wait_and_all_native_prerequisites_closed(ctx, ineligible):
    ctx.tracker.close_issue(50)
    if ineligible == "one-open":
        ctx.tracker.add_issue(51, "Still open")
        ctx.tracker.add_blocked_by(1, 51)
    elif ineligible == "no-edges":
        ctx.tracker.remove_blocked_by(1, 50)
    elif ineligible in {"no-wait", "empty-wait"}:
        ctx.tracker.update_issue(1, title="Work", body="## Waiting\n" if ineligible == "empty-wait" else "work")
    else:
        ctx.tracker.close_issue(1)
    scan(ctx)
    assert orch_inbox.requests(ctx.store) == []


def test_unchanged_done_notice_survives_restart_but_changed_wait_can_notify(ctx):
    ctx.tracker.close_issue(50)
    scan(ctx)
    row, = orch_inbox.requests(ctx.store)
    orch_inbox.set_request_status(ctx.store, row["id"], "done")
    ctx.store = StateStore(ctx.store.root, now=ctx.now)
    scan(ctx)
    scan(ctx)
    assert len(orch_inbox.requests(ctx.store)) == 1
    ctx.tracker.update_issue(1, title="Work", body="## Waiting\n" + WAIT.replace("Catalog review", "New review"))
    scan(ctx)
    rows = orch_inbox.requests(ctx.store)
    assert len(rows) == 2 and rows[0]["wait_resume_key"] != rows[1]["wait_resume_key"]


def test_unchanged_refresh_reuses_blockers_but_closed_blocker_invalidates_cache(ctx, monkeypatch):
    original = ctx.remote.blocked_by
    calls = []
    monkeypatch.setattr(ctx.remote, "blocked_by", lambda n: calls.append(n) or original(n))
    scan(ctx)
    assert calls  # Refresh itself also reads native edges when their listing facts change.
    calls.clear()
    scan(ctx)
    scan(ctx, refresh=False)
    assert calls == []
    ctx.tracker.close_issue(50)
    scan(ctx)
    assert calls and len(orch_inbox.requests(ctx.store)) == 1


def test_scan_uses_the_owner_reader(ctx, monkeypatch):
    ctx.tracker.close_issue(50)
    monkeypatch.setattr(order_waiting, "section", lambda body: None)
    scan(ctx)
    assert orch_inbox.requests(ctx.store) == []


def test_old_listing_without_successful_refresh_cannot_deliver(ctx):
    ctx.tracker.close_issue(50)
    scan(ctx, refresh=False)
    assert orch_inbox.requests(ctx.store) == []
    scan(ctx)
    assert len(orch_inbox.requests(ctx.store)) == 1


def test_failed_delivery_is_visible_and_retryable(ctx, monkeypatch):
    ctx.tracker.close_issue(50)
    with monkeypatch.context() as patch:
        patch.setattr(orch_inbox, "add_request", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
        report = scan(ctx)
    assert len(report.actions) == 1 and "waiting_resume" in report.actions[0] and "disk full" in report.actions[0]
    assert orch_inbox.requests(ctx.store) == []
    scan(ctx)
    assert len(orch_inbox.requests(ctx.store)) == 1


def test_one_failed_issue_does_not_starve_other_waits(ctx, monkeypatch):
    ctx.tracker.add_issue(2, "Another wait", "## Waiting\n" + WAIT)
    ctx.tracker.add_blocked_by(2, 50)
    ctx.tracker.close_issue(50)
    original = scheduler._blockers
    def read(context, number, **kwargs):
        if number == 1:
            raise TrackerError("HTTP 502: blocker read failed")
        return original(context, number, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(scheduler, "_blockers", read)
        report = scan(ctx)
    assert len(report.actions) == 1 and "blocker read failed" in report.actions[0]
    row, = orch_inbox.requests(ctx.store)
    assert row["issue"] == 2
    scan(ctx)
    assert sorted(row["issue"] for row in orch_inbox.requests(ctx.store)) == [1, 2]


def test_rate_limit_still_reaches_the_tick_pause(ctx, monkeypatch):
    monkeypatch.setattr(scheduler, "_blockers", lambda *a, **kw: (_ for _ in ()).throw(TrackerError("HTTP 429")))
    with pytest.raises(TrackerError, match="429"):
        scan(ctx)
