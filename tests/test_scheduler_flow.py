"""Ready reasons recorded from the tick's existing branches, beyond the locked flow-record tests."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from foragent import capacity, orch_plan, scheduler, scheduler_flow
from foragent.capacity import write_state
from foragent.tracker import Dependency
from test_scheduler import BODY, approve, ctx  # noqa: F401


def ready_rows(context, number):
    return [row for row in context.store.flow_records() if row["kind"] == "ready" and row["issue"] == number]


def reasons(context, number):
    return [row["data"]["reason"] for row in ready_rows(context, number)]


def prepare(context, budget=5, **config):
    context.config = replace(context.config, max_parallel=None, skill_libraries=[], **config)
    context.start_runs = True
    write_state(context.store, budget, context.now())
    starts = []

    def launch(number=None, *, kind="issue", lane=None):
        if kind == "issue":
            starts.append(number)
            return 7000 + number
        return None

    context.launcher = launch
    return starts


def ready_issue(context, number, *, running=False):
    body = BODY.replace("## Done when", f"## Files\n- src/f{number}.py\n## Done when")
    approve(context, number, body=body, labels=["running" if running else "ready"])
    if running:
        context.store.start_run(number, branch=f"foragent/issue-{number}",
                                worktree=context.store.worktree_path(number), pid=7000 + number)


def test_every_candidate_behind_the_slot_break_gets_the_known_cause_without_any_read(ctx, monkeypatch):
    starts = prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    ready_issue(ctx, 3)
    reads = []
    original = ctx.tracker.get_issue
    assert scheduler.tick(ctx).skipped is None
    monkeypatch.setattr(ctx.tracker, "get_issue", lambda number: reads.append(number) or original(number))
    assert scheduler.tick(ctx).skipped is None
    assert starts == [] and reads == [], "A break is decisive: later checks stay skipped"
    for number in (2, 3):
        assert reasons(ctx, number) == ["slots-full"]
        assert ready_rows(ctx, number)[0]["data"]["run_slots"] == {"limit": 1, "occupied": 1, "merge_waiters": 0}


def test_slots_full_after_a_start_covers_the_rest_and_the_started_one_clears(ctx):
    starts = prepare(ctx, 1)
    ready_issue(ctx, 1)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    assert starts == [1]
    started = ready_rows(ctx, 1)
    assert [(row["data"]["reason"], row["data"]["outcome"]) for row in started] == [(None, "started")]
    assert started[0]["run_started_at"] == ctx.store.get_run(1).started_at
    assert reasons(ctx, 2) == ["slots-full"]


def test_project_ceiling_is_the_scope_where_it_actually_bound(ctx):
    prepare(ctx, 5)
    ctx.config = replace(ctx.config, max_parallel=1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    slots = ready_rows(ctx, 2)[0]["data"]["run_slots"]
    assert (slots["limit"], slots["occupied"]) == (1, 1), "The budget of 5 did not bind"


def test_decisive_fact_change_is_a_new_record(ctx):
    prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    (ctx.store.root / "merge-queue.jsonl").write_text(
        json.dumps({"n": 1, "event": "ready", "started_at": ctx.store.get_run(1).started_at, "target": "main"}) + "\n",
        encoding="utf-8")
    assert scheduler.tick(ctx).skipped is None
    assert scheduler.tick(ctx).skipped is None
    rows = ready_rows(ctx, 2)
    assert [row["data"]["run_slots"]["merge_waiters"] for row in rows] == [0, 1]


def test_unreadable_merge_waiters_are_unknown_never_zero(ctx):
    prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    (ctx.store.root / "merge-queue.jsonl").mkdir(parents=True)
    assert scheduler.tick(ctx).skipped is None
    assert ready_rows(ctx, 2)[0]["data"]["run_slots"]["merge_waiters"] is None


def test_free_slot_is_measured_and_only_when_plan_width_holds_the_candidate(ctx):
    starts = prepare(ctx, 3, lanes={"orch": {"paths": ["src/**"]}})
    approve(ctx, 1, body=BODY, labels=["running", "lane:orch"])
    ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.store.worktree_path(1), lane="orch", pid=7001)
    approve(ctx, 2, body=BODY, labels=["ready", "lane:orch"])
    orch_plan.set_plan(ctx.store, {"orch": 1}, ctx.now() + timedelta(hours=1))
    assert scheduler.tick(ctx).skipped is None
    assert starts == []
    row = ready_rows(ctx, 2)[0]["data"]
    assert row["reason"] == "free-slot" and row["run_slots"]["limit"] > row["run_slots"]["occupied"]


@pytest.mark.parametrize("reason", ["starts-off", "outside-run-hours", "outbox-wait", "github-wait", "memory-hold"])
def test_global_branches_hold_all_candidates_with_their_own_reason(ctx, monkeypatch, reason):
    starts = prepare(ctx, 3)
    for number in (1, 2):
        ready_issue(ctx, number)
    if reason == "starts-off":
        ctx.start_runs = False
    elif reason == "outside-run-hours":
        monkeypatch.setattr(scheduler, "in_run_hours", lambda now, hours: False)
    elif reason == "outbox-wait":
        monkeypatch.setattr(ctx.store, "github_blocking_outbox", lambda: [{"issue": 9}])
    elif reason == "github-wait":
        ctx.tracker.list_issues()
        ctx.store.ingest(ctx.tracker.list_issues(), ctx.now(), {})
        ctx.store.pause_github(ctx.now() + timedelta(hours=1))
    else:
        monkeypatch.setattr(scheduler, "machine_load", lambda: capacity.LoadSample("critical", 0.5, 60))
    scheduler.tick(ctx)
    assert starts == []
    assert reasons(ctx, 1) == [reason] and reasons(ctx, 2) == [reason]


def test_blocked_by_names_open_edges_and_issue_state_keeps_every_edge(ctx):
    starts = prepare(ctx, 3)
    ready_issue(ctx, 1)
    ctx.tracker.set_blocked_by(1, [Dependency(8, "open"), Dependency(9, "closed")])
    assert scheduler.tick(ctx).skipped is None
    assert starts == []
    row = ready_rows(ctx, 1)[0]["data"]
    assert row["reason"] == "blocked-by"
    assert [edge["number"] for edge in row["blocked_by"]] == [8]
    edges = [r["data"]["blocked_by"] for r in ctx.store.flow_records()
             if r["kind"] == "issue-state" and r["issue"] == 1][-1]
    assert {(edge["number"], edge["state"]) for edge in edges} == {(8, "open"), (9, "closed")}


def test_a_failed_prerequisite_without_open_edges_is_not_called_blocked_by(ctx):
    prepare(ctx, 3)
    ready_issue(ctx, 1)
    ctx.tracker.create_pr(head="foragent/issue-1", base="main", title="t", body="b")
    assert scheduler.tick(ctx).skipped is None
    assert reasons(ctx, 1) == ["prerequisite"]


@pytest.mark.parametrize("label", ["blocked", "needs-human"])
def test_held_issue_names_its_label_and_release_is_one_record(ctx, label):
    prepare(ctx, 3)
    ready_issue(ctx, 1)
    ctx.tracker.add_labels(1, [label])
    for _ in range(2):
        assert scheduler.tick(ctx).skipped is None
    rows = ready_rows(ctx, 1)
    assert [row["data"]["reason"] for row in rows] == ["held-no-retry"]
    assert rows[0]["data"]["held_by"] == [label]
    ctx.tracker.close_issue(1)
    ctx.store.issues_refresh_due = lambda now: True
    for _ in range(2):
        assert scheduler.tick(ctx).skipped is None
    assert reasons(ctx, 1) == ["held-no-retry", "released"]


def test_a_new_run_of_the_same_issue_is_a_new_start_record(ctx):
    prepare(ctx, 3)
    ready_issue(ctx, 1)
    assert scheduler.tick(ctx).skipped is None
    for _ in range(2):  # A replayed observation of the second start adds one record.
        ctx.store._record_flow_changes("ready", [(1, "second-run", {"reason": None, "outcome": "started"})])
    assert [row["run_started_at"] for row in ready_rows(ctx, 1)][1:] == ["second-run"]


def test_recording_failure_keeps_the_start_and_the_report(ctx, monkeypatch):
    starts = prepare(ctx, 3)
    ready_issue(ctx, 1)
    monkeypatch.setattr(scheduler_flow, "_flush", lambda context, tick: 1 / 0)
    report = scheduler.tick(ctx)
    assert report.skipped is None and starts == [1]
    assert any("#1 started running" in line for line in report.actions)


def test_a_busy_tick_records_nothing(ctx):
    prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    with ctx.store.tick_lock() as held:
        assert held
        assert scheduler.tick(ctx).skipped == scheduler.BUSY_TICK
    assert [row for row in ctx.store.flow_records() if row["kind"] == "ready"] == []


def force_refresh(context):
    context.store.issues_refresh_due = lambda now: True


def test_ready_removed_with_approval_kept_leaves_the_candidates(ctx):
    prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    body = BODY.replace("## Done when", "## Files\n- src/f2.py\n## Done when")
    approve(ctx, 2, body=body)
    assert scheduler.tick(ctx).skipped is None  # Promotes it; the slot is taken.
    assert reasons(ctx, 2) == ["slots-full"]
    ctx.tracker._tracker.remove_labels(2, ["ready"])  # A person's removal: this approval never promotes again.
    force_refresh(ctx)
    for _ in range(2):
        assert scheduler.tick(ctx).skipped is None
    assert "ready" not in ctx.tracker.get_issue(2).labels
    assert reasons(ctx, 2) == ["slots-full", "released"]


def test_integrity_rejection_replaces_the_obsolete_reason(ctx):
    prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    ctx.tracker.update_issue(2, title="Issue 2", body=BODY + "\nchanged")
    assert scheduler.tick(ctx).skipped is None
    assert reasons(ctx, 2)[:2] == ["slots-full", "integrity"]


def test_snapshot_rejection_names_its_cause(ctx, monkeypatch):
    prepare(ctx, 3)
    ready_issue(ctx, 1)
    monkeypatch.setattr(ctx.tracker._tracker, "body_last_edited_at",
                        lambda number: (ctx.now() + timedelta(days=1)).isoformat())
    assert scheduler.tick(ctx).skipped is None
    assert reasons(ctx, 1)[0] == "approval-rejected"


@pytest.mark.parametrize("actor", [None, "stranger"])
def test_ready_without_a_valid_approval_event_names_its_cause(ctx, actor):
    starts = prepare(ctx, 3)
    ctx.tracker.add_issue(1, "Issue 1", BODY, labels=["ready"])
    if actor:
        ctx.tracker.record_label(1, "approved", actor)
    assert scheduler.tick(ctx).skipped is None
    assert starts == [] and reasons(ctx, 1) == ["no-approval"]


def test_an_unapproved_issue_that_never_waited_is_not_a_candidate(ctx):
    prepare(ctx, 3)
    ctx.tracker.add_issue(1, "Issue 1", BODY)
    assert scheduler.tick(ctx).skipped is None
    assert ready_rows(ctx, 1) == []


def fail_listing(context, monkeypatch):
    from foragent.tracker import TrackerError

    def failed(*args, **kwargs):
        raise TrackerError("gh: unavailable (HTTP 502)")

    monkeypatch.setattr(context.tracker._tracker, "list_issues", failed)
    force_refresh(context)
    return failed


def test_refresh_failure_without_a_stored_approval_names_its_cause(ctx, monkeypatch):
    prepare(ctx, 3)
    ready_issue(ctx, 1)
    ctx.tracker.list_issues()  # Stored, but no approval snapshot yet.
    fail_listing(ctx, monkeypatch)
    assert scheduler.tick(ctx).skipped is None
    assert reasons(ctx, 1) == ["refresh-failed"]


def test_a_deferred_issue_read_keeps_it_a_candidate_with_that_cause(ctx, monkeypatch):
    prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    failed = fail_listing(ctx, monkeypatch)
    monkeypatch.setattr(ctx.tracker._tracker, "label_events", failed)
    ctx.store.record_read = lambda *args, **kwargs: None
    assert scheduler.tick(ctx).skipped is None
    assert reasons(ctx, 2) == ["slots-full", "read-deferred"], "A deferral is not a release"


def test_a_tick_that_failed_before_judging_releases_nothing_and_names_the_failure(ctx, monkeypatch):
    prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    monkeypatch.setattr(scheduler.board_parents, "order_parents", lambda rows: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        scheduler.tick(ctx)
    assert reasons(ctx, 2) == ["slots-full", "tick-failed"]


def test_a_tick_that_failed_after_judging_does_not_release_its_ready_candidates(ctx, monkeypatch):
    prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    monkeypatch.setattr(scheduler, "_start_ready", lambda *args: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        scheduler.tick(ctx)
    assert reasons(ctx, 2) == ["slots-full", "tick-failed"], "Still a candidate, not released; this tick's failure held it"


def moving_clock(context):
    clock = [context.now()]
    context.now = lambda: clock[0]
    context.tracker._now = lambda: clock[0]
    return clock


def test_invalidated_approval_then_held_then_reapproval_during_failed_refresh(ctx, monkeypatch):
    prepare(ctx, 1)
    clock = moving_clock(ctx)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    ctx.tracker.update_issue(2, title="Issue 2", body=BODY + "\nchanged")
    clock[0] += timedelta(minutes=1)
    assert scheduler.tick(ctx).skipped is None  # Rejected: the body changed after approval.
    clock[0] += timedelta(minutes=1)
    assert scheduler.tick(ctx).skipped is None  # Flagged and older than the flag: no retry.
    rows = ready_rows(ctx, 2)
    assert [row["data"]["reason"] for row in rows] == ["slots-full", "integrity", "held-no-retry"]
    assert rows[-1]["data"]["held_by"] == ["blocked"]
    clock[0] += timedelta(minutes=1)
    ctx.tracker._tracker.record_label(2, "approved", "human")
    ctx.store.update_local_issue(2, reads={})
    fail_listing(ctx, monkeypatch)
    assert scheduler.tick(ctx).skipped is None
    assert reasons(ctx, 2)[-1] == "refresh-failed", "New evidence waits for a successful refresh"


def test_repaired_orphan_during_a_failed_refresh_waits_with_that_cause(ctx, monkeypatch):
    prepare(ctx, 3)
    approve(ctx, 1, labels=["running"])
    ctx.tracker.list_issues()
    fail_listing(ctx, monkeypatch)
    assert scheduler.tick(ctx).skipped is None
    assert reasons(ctx, 1) == ["refresh-failed"]


def test_hash_mismatch_flag_older_than_the_approval_evidence_holds_without_retry(ctx):
    from foragent.reasons import Reason

    prepare(ctx, 3)
    ready_issue(ctx, 1)
    ctx.tracker.list_issues()
    scheduler.flag(ctx, 1, Reason.HASH_MISMATCH, "Approval invalidated")
    assert scheduler.tick(ctx).skipped is None
    rows = ready_rows(ctx, 1)
    assert [row["data"]["reason"] for row in rows] == ["held-no-retry"] and rows[0]["data"]["held_by"] == ["blocked"]


def test_hash_mismatch_flag_without_any_approval_event_holds_without_retry(ctx):
    from foragent.reasons import Reason

    prepare(ctx, 3)
    ctx.tracker.add_issue(1, "Issue 1", BODY, labels=["ready"])
    ctx.tracker.list_issues()
    scheduler.flag(ctx, 1, Reason.HASH_MISMATCH, "Approval invalidated")
    assert scheduler.tick(ctx).skipped is None
    assert reasons(ctx, 1) == ["held-no-retry"]


def test_recovered_run_during_a_failed_refresh_waits_with_that_cause(ctx, monkeypatch):
    starts = prepare(ctx, 3)
    ready_issue(ctx, 1, running=True)
    ctx.tracker.list_issues()
    ctx.process_table.exit(7001)
    fail_listing(ctx, monkeypatch)
    assert scheduler.tick(ctx).skipped is None
    assert starts == [] and ctx.store.get_run(1).status != "running"
    assert reasons(ctx, 1) == ["refresh-failed"]


@pytest.mark.parametrize("error,reason", [
    (lambda: __import__("foragent.tracker").tracker.TrackerError("gh: unavailable (HTTP 502)"), "read-failed"),
    (lambda: ValueError("bad data"), "tick-failed"),
], ids=["tracker", "other"])
def test_an_exception_while_judging_replaces_the_obsolete_reason(ctx, monkeypatch, error, reason):
    prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    original = scheduler._check_prerequisites

    def failing(context, issue, *args, **kwargs):
        if issue.number == 2:
            raise error()
        return original(context, issue, *args, **kwargs)

    monkeypatch.setattr(scheduler, "_check_prerequisites", failing)
    with pytest.raises(Exception) as raised:
        scheduler.tick(ctx)
    assert type(raised.value) is type(error()), "The original error stays the tick's"
    rows = ready_rows(ctx, 2)
    assert [row["data"]["reason"] for row in rows] == ["slots-full", reason]
    assert rows[-1]["data"]["error"] == type(error()).__name__


def test_a_rate_limit_while_judging_a_not_yet_ready_issue_names_the_wait(ctx, monkeypatch):
    from foragent.tracker import TrackerError

    prepare(ctx, 3)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    approve(ctx, 3, body=BODY)  # Approved, not yet ready: no READY row would cover it.

    original = ctx.tracker._tracker.label_events

    def limited(number):
        if number == 3:
            raise TrackerError("API rate limit exceeded (HTTP 429)")
        return original(number)

    monkeypatch.setattr(ctx.tracker._tracker, "label_events", limited)
    force_refresh(ctx)
    scheduler.tick(ctx)
    assert reasons(ctx, 3) == ["github-wait"]


def test_a_start_failure_after_checks_names_it_for_the_issue_being_started(ctx, monkeypatch):
    prepare(ctx, 3)
    ready_issue(ctx, 1)

    def broken(number=None, **kwargs):
        raise OSError("cannot launch")

    ctx.launcher = broken
    with pytest.raises(OSError):
        scheduler.tick(ctx)
    assert reasons(ctx, 1) == ["tick-failed"]


def test_a_failure_during_a_read_in_the_start_loop_replaces_the_reason(ctx, monkeypatch):
    from foragent.tracker import TrackerError

    prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    ctx.store.finish_run(1, outcome="merged")
    ctx.tracker.close_issue(1)
    ctx.process_table.exit(7001)

    def unreadable(number):
        raise TrackerError("gh: unavailable (HTTP 502)")

    monkeypatch.setattr(ctx.tracker._tracker, "get_issue", unreadable)
    with pytest.raises(TrackerError):
        scheduler.tick(ctx)
    assert reasons(ctx, 2) == ["slots-full", "read-failed"]


@pytest.mark.parametrize("name", ["fetch_base", "_recover_runs", "_reconcile_batches", "_github_wait", "_apply_budgets"])
def test_a_failure_outside_any_issues_judgment_holds_the_known_candidates(ctx, monkeypatch, name):
    _waiting_candidate(ctx)
    ctx.store.finish_run(1, outcome="merged")  # The old cause is gone, so slots-full would now be a false claim.
    ctx.tracker.close_issue(1)
    ctx.process_table.exit(7001)
    monkeypatch.setattr(scheduler, name, lambda *args, **kwargs: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        scheduler.tick(ctx)
    rows = ready_rows(ctx, 2)
    assert [row["data"]["reason"] for row in rows] == ["slots-full", "tick-failed"]
    assert rows[-1]["data"]["error"] == "ZeroDivisionError"
    assert ready_rows(ctx, 1) == [], "The failure holds candidates only"


def test_a_decisive_hold_is_not_replaced_by_a_failure_after_it(ctx, monkeypatch):
    _waiting_candidate(ctx)
    monkeypatch.setattr(scheduler.orch_dream, "start", lambda *args: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        scheduler.tick(ctx)
    assert reasons(ctx, 2) == ["slots-full"]


@pytest.mark.parametrize("exit_path", ["closed", "parent", "revoked"])
def test_a_candidate_judged_out_stays_released_when_a_later_issue_fails(ctx, monkeypatch, exit_path):
    _waiting_candidate(ctx)
    ready_issue(ctx, 3)
    assert scheduler.tick(ctx).skipped is None
    if exit_path == "closed":
        ctx.tracker.close_issue(2)
    elif exit_path == "parent":
        monkeypatch.setattr(scheduler.board_parents, "order_parents", lambda rows: {2})
    else:
        ctx.tracker.remove_labels(2, ["approved", "ready"])
    original = scheduler._check_prerequisites

    def failing(context, issue, *args, **kwargs):
        if issue.number == 3:
            raise ValueError("later issue")
        return original(context, issue, *args, **kwargs)

    monkeypatch.setattr(scheduler, "_check_prerequisites", failing)
    with pytest.raises(ValueError):
        scheduler.tick(ctx)
    assert reasons(ctx, 2) == ["slots-full", "released"]
    assert reasons(ctx, 3) == ["slots-full", "tick-failed"]


def test_a_start_is_kept_when_the_issue_goes_missing_right_after_it(ctx):
    starts = prepare(ctx, 3)
    ready_issue(ctx, 1)
    ctx.store.record_flow("ready", issue=1, reason="slots-full", outcome="waiting")
    launch = ctx.launcher

    def vanish(number=None, **kwargs):
        pid = launch(number, **kwargs)
        ctx.store.ingest_absence(1, ctx.now() + timedelta(seconds=1))
        return pid

    ctx.launcher = vanish
    assert scheduler.tick(ctx).skipped is None
    assert starts == [1] and 1 not in {row["number"] for row in ctx.store.local_issues()}
    rows = ready_rows(ctx, 1)
    assert [(row["data"]["reason"], row["data"]["outcome"]) for row in rows] == [("slots-full", "waiting"), (None, "started")]
    assert rows[-1]["run_started_at"] == ctx.store.get_run(1).started_at


def test_an_exception_on_an_issue_that_is_no_candidate_records_no_wait(ctx, monkeypatch):
    prepare(ctx, 3)
    ctx.tracker.add_issue(1, "Issue 1", BODY)
    monkeypatch.setattr(scheduler.board, "sync_status", lambda *args, **kwargs: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        scheduler.tick(ctx)
    assert ready_rows(ctx, 1) == []


def _waiting_candidate(ctx):
    prepare(ctx, 1)
    ready_issue(ctx, 1, running=True)
    ready_issue(ctx, 2)
    assert scheduler.tick(ctx).skipped is None
    assert reasons(ctx, 2) == ["slots-full"]


def _not_found(context, monkeypatch, number):
    from foragent.tracker import TrackerError

    original = context.tracker._tracker.get_issue

    def missing(n):
        if n == number:
            raise TrackerError("gh: issue not found (HTTP 404)")
        return original(n)

    monkeypatch.setattr(context.tracker._tracker, "get_issue", missing)


def test_a_404_at_the_start_check_removes_the_row_and_releases_the_candidate(ctx, monkeypatch):
    _waiting_candidate(ctx)
    ctx.store.finish_run(1, outcome="merged")
    ctx.tracker.close_issue(1)
    ctx.process_table.exit(7001)
    _not_found(ctx, monkeypatch, 2)
    with pytest.raises(Exception, match="HTTP 404"):
        scheduler.tick(ctx)
    assert 2 not in {row["number"] for row in ctx.store.local_issues()}
    assert reasons(ctx, 2) == ["slots-full", "released"]


def test_a_404_while_judging_before_the_candidates_are_known_releases_too(ctx, monkeypatch):
    _waiting_candidate(ctx)
    ctx.store.clear_approval(2)
    _not_found(ctx, monkeypatch, 2)
    with pytest.raises(Exception, match="HTTP 404"):
        scheduler.tick(ctx)
    assert reasons(ctx, 2) == ["slots-full", "released"]


def test_a_deferred_404_during_a_failed_refresh_releases_instead_of_naming_the_deferral(ctx, monkeypatch):
    _waiting_candidate(ctx)
    ctx.store.finish_run(1, outcome="merged")
    ctx.tracker.close_issue(1)
    ctx.process_table.exit(7001)
    fail_listing(ctx, monkeypatch)
    _not_found(ctx, monkeypatch, 2)
    assert scheduler.tick(ctx).skipped is None, "The deferral keeps the tick going"
    assert reasons(ctx, 2) == ["slots-full", "released"]


def test_a_row_gone_with_no_wait_before_it_records_nothing(ctx, monkeypatch):
    prepare(ctx, 3)
    ready_issue(ctx, 2)
    ctx.tracker.list_issues()
    _not_found(ctx, monkeypatch, 2)
    with pytest.raises(Exception, match="HTTP 404"):
        scheduler.tick(ctx)
    assert ready_rows(ctx, 2) == []
