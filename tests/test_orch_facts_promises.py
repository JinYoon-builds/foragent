"""Unit boundaries of the closed-promise observer (D122).

Failures come from the gate's baseline captures (JUnit failures and errors); a CI report lists only
collected and passing ids, so it is evidence of passes and its non-passing tests say nothing (#2951).
"""

from datetime import UTC, datetime
import json
from types import SimpleNamespace

from foragent import brief_report, orch_facts_promises, orch_inbox
from foragent.state import StateStore

TEST = "tests/acceptance/test_shipped.py::test_shipped"
OTHER = "tests/acceptance/test_other.py::test_other"
NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)


def world(tmp_path, states):
    store = StateStore(tmp_path / "state", lambda: NOW)
    for number, path in ((7, TEST), (8, OTHER)):
        store.save_approval(number, body="b", body_sha="s", acceptance_blobs={path.split("::")[0]: "x"},
                            approved_by="human", approved_at=NOW.isoformat())
    tracker = SimpleNamespace(get_issue=lambda n: SimpleNamespace(state=states[n]))
    return SimpleNamespace(store=store, tracker=tracker, config=SimpleNamespace(acceptance_dir="tests/acceptance/"))


def reports(passing, base="b" * 40):
    ids = [TEST, OTHER]
    return [{"report": {"base_sha": base, "base": {"complete": True, "collected_ids": ids,
                                                     "passing_ids": list(passing)}}}]


def promise_requests(ctx):
    return [row for row in orch_inbox.requests(ctx.store) if row.get("promise")]


def cleared(ctx):
    path = ctx.store.root / "orch-events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row["kind"] == "promise-cleared"]


def test_closed_owner_reported_once_open_owner_never(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, NEW], {7: "closed", 8: "open"})
    capture(ctx, "a", OLD, failing=[TEST, OTHER])
    orch_facts_promises.observe_baselines(ctx)
    capture(ctx, "b", NEW, failing=[TEST, OTHER], created="2026-11-06T11:20:00+00:00")
    orch_facts_promises.observe_baselines(ctx)
    rows = promise_requests(ctx)
    assert [row["promise"]["test"] for row in rows] == [TEST]
    assert rows[0]["promise"]["issue"] == 7 and "#7" in rows[0]["text"]
    assert list(orch_facts_promises.open_promises(ctx.store)) == [TEST]


def test_passing_reading_clears_once_and_leaves_the_report(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, NEW])
    capture(ctx, "a", OLD, passing=[OTHER], failing=[TEST])
    orch_facts_promises.observe_baselines(ctx)
    lines = brief_report.render_short(brief_report.collect(ctx.store, NOW))
    assert any("test_shipped" in line and "#7" in line for line in lines.splitlines())
    orch_facts_promises.observe(ctx, reports([TEST, OTHER]))
    orch_facts_promises.observe(ctx, reports([TEST, OTHER]))
    assert [(row["issue"], row["test"]) for row in cleared(ctx)] == [(7, TEST)]
    assert len(promise_requests(ctx)) == 1
    assert orch_facts_promises.open_promises(ctx.store) == {}
    assert "test_shipped" not in brief_report.render_short(brief_report.collect(ctx.store, NOW))


def test_unknown_owner_state_and_unreadable_reports_report_nothing(tmp_path):
    def unknown(n):
        raise RuntimeError("GitHub down")

    ctx = world(tmp_path, {})
    ctx.tracker = SimpleNamespace(get_issue=unknown)
    orch_facts_promises.observe(ctx, reports([]))
    orch_facts_promises.observe(ctx, [{"report": {"base": "garbage"}}])
    assert promise_requests(ctx) == []


class Git:
    """Main's order: each later base descends from every earlier one."""

    def __init__(self, order):
        self.order = order

    def is_ancestor(self, ancestor, head):
        return self.order.index(ancestor) <= self.order.index(head)


OLD, NEW = "a" * 40, "b" * 40


def test_an_older_main_passing_after_a_newer_failure_neither_clears_nor_reports_twice(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, NEW])
    capture(ctx, "new", NEW, passing=[OTHER], failing=[TEST])
    orch_facts_promises.observe_baselines(ctx)
    orch_facts_promises.observe(ctx, reports([TEST, OTHER], base=OLD))  # a late reading of older main
    assert cleared(ctx) == [] and list(orch_facts_promises.open_promises(ctx.store)) == [TEST]
    orch_facts_promises.observe_baselines(ctx)
    assert len(promise_requests(ctx)) == 1


def test_an_older_main_failing_after_a_newer_pass_reports_nothing(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, NEW])
    capture(ctx, "old", OLD, passing=[OTHER], failing=[TEST])
    orch_facts_promises.observe_baselines(ctx)
    orch_facts_promises.observe(ctx, reports([TEST, OTHER], base=NEW))
    capture(ctx, "old-late", OLD, passing=[OTHER], failing=[TEST])  # a late reading of older main
    orch_facts_promises.observe_baselines(ctx)
    assert len(promise_requests(ctx)) == 1 and len(cleared(ctx)) == 1
    assert orch_facts_promises.open_promises(ctx.store) == {}


def test_merge_batch_reading_observes_main_even_when_its_tip_loses_tests(tmp_path, monkeypatch):
    from foragent import ci_mode, merge_owner_batch

    ctx = baseline_world(tmp_path, [OLD, NEW])
    capture(ctx, "old", OLD, passing=[OTHER], failing=[TEST])
    orch_facts_promises.observe_baselines(ctx)
    ctx.repo = "owner/repo"
    ctx.config.ci_shards = 1
    found = reports([TEST, OTHER])  # main passes the locked test again, though the tip lost OTHER
    evidence = merge_owner_batch.Evidence("base", [], None, 0, excluded=[])
    monkeypatch.setattr(evidence, "reports", lambda ctx: ("pass", NEW, "tip", found))
    monkeypatch.setattr(merge_owner_batch, "measurement_scope", lambda *args: {})
    monkeypatch.setattr(ci_mode, "comparison", lambda *args, **kwargs: ([OTHER], 0))
    monkeypatch.setattr(ci_mode, "scope_covers", lambda recorded, required: True)
    found[0]["report"]["scope"] = {}
    assert evidence.outcome(ctx) == "fail"
    assert [row["test"] for row in cleared(ctx)] == [TEST]
    assert orch_facts_promises.open_promises(ctx.store) == {}


# ---- #2925: the gate's own baseline captures, read without a cursor


def capture(ctx, name, base, *, passing=(), failing=(), collected=None, created="2026-11-06T11:00:00+00:00"):
    """One baseline capture file of ``base`` in the machine-wide cache."""
    cache = ctx.store.root.parent / "baselines"
    cache.mkdir(parents=True, exist_ok=True)
    collected = sorted(set(passing) | set(failing)) if collected is None else collected
    (cache / f"{name}.json").write_text(json.dumps({
        "base_sha": base, "created_at": created, "node_ids": list(passing), "failing_ids": list(failing),
        "collected_ids": collected, "repository": str(ctx.repo_root.resolve()), "timed_out": False,
    }), encoding="utf-8")


def baseline_world(tmp_path, order, states=None):
    states = states or {7: "closed", 8: "closed"}
    ctx = world(tmp_path, states)
    ctx.repo_root = tmp_path
    ctx.config.repo, ctx.config.base_branch = "owner/repo", "main"
    ctx.git = Git(order)
    # The tick reads no GitHub: the owners' states come from local rows.
    from foragent.tracker import Issue

    ctx.store.ingest([Issue(number=n, title="t", body="b", labels=frozenset(), state=states[n]) for n in (7, 8)], NOW, {})
    return ctx


MID = "m" * 40


def test_an_older_failing_capture_finishing_after_a_newer_partial_one_is_still_reported(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, NEW])
    capture(ctx, "newer", NEW, passing=[OTHER], created="2026-11-06T11:30:00+00:00")  # TEST not collected
    orch_facts_promises.observe_baselines(ctx)
    assert promise_requests(ctx) == []
    # The older capture finishes later and carries an earlier time stamp.
    capture(ctx, "older", OLD, passing=[OTHER], failing=[TEST], created="2026-11-06T11:10:00+00:00")
    orch_facts_promises.observe_baselines(ctx)
    rows = promise_requests(ctx)
    assert [(row["promise"]["test"], row["promise"]["base_sha"]) for row in rows] == [(TEST, OLD)]


def test_a_newer_red_commit_seen_first_moves_back_to_the_first_recorded_red_commit(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, NEW])
    capture(ctx, "newer", NEW, passing=[OTHER], failing=[TEST], created="2026-11-06T11:30:00+00:00")
    orch_facts_promises.observe_baselines(ctx)  # the newer red main is read first
    assert [row["promise"]["base_sha"] for row in promise_requests(ctx)] == [NEW]
    capture(ctx, "older", OLD, passing=[OTHER], failing=[TEST])
    orch_facts_promises.observe_baselines(ctx)
    rows = promise_requests(ctx)
    assert len(rows) == 1 and rows[0]["promise"]["base_sha"] == OLD and OLD[:12] in rows[0]["text"]
    assert orch_facts_promises.open_promises(ctx.store)[TEST]["base_sha"] == OLD


def test_skipped_or_xfailed_locked_test_in_a_successful_capture_is_no_failure(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, NEW])
    capture(ctx, "skipped", OLD, passing=[OTHER], failing=[], collected=[TEST, OTHER])
    orch_facts_promises.observe_baselines(ctx)
    assert promise_requests(ctx) == []


def test_a_skip_inside_a_red_run_neither_clears_nor_restarts_it(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, MID, NEW])
    capture(ctx, "a", OLD, passing=[OTHER], failing=[TEST])
    capture(ctx, "b", MID, passing=[OTHER], collected=[TEST, OTHER])  # skipped: no evidence
    capture(ctx, "c", NEW, passing=[OTHER], failing=[TEST])
    orch_facts_promises.observe_baselines(ctx)
    assert [row["promise"]["base_sha"] for row in promise_requests(ctx)] == [OLD]
    assert cleared(ctx) == []


def test_a_ci_pass_on_a_newer_main_clears_and_older_red_captures_do_not_raise_again(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, MID, NEW])
    capture(ctx, "a", OLD, passing=[OTHER], failing=[TEST])
    orch_facts_promises.observe_baselines(ctx)
    orch_facts_promises.observe(ctx, reports([TEST, OTHER], base=MID))  # CI: passes again
    assert len(cleared(ctx)) == 1 and orch_facts_promises.open_promises(ctx.store) == {}
    orch_facts_promises.observe_baselines(ctx)
    orch_facts_promises.observe_baselines(ctx)
    assert len(promise_requests(ctx)) == 1
    capture(ctx, "c", NEW, passing=[OTHER], failing=[TEST])  # red anew after the pass
    orch_facts_promises.observe_baselines(ctx)
    assert [row["promise"]["base_sha"] for row in promise_requests(ctx)] == [OLD, NEW]


def test_red_pass_red_read_together_clears_the_old_report_and_raises_the_new_run(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, MID, NEW])
    capture(ctx, "a", OLD, passing=[OTHER], failing=[TEST])
    orch_facts_promises.observe_baselines(ctx)
    capture(ctx, "b", MID, passing=[TEST, OTHER])
    capture(ctx, "c", NEW, passing=[OTHER], failing=[TEST])
    orch_facts_promises.observe_baselines(ctx)
    assert [row["promise"]["base_sha"] for row in promise_requests(ctx)] == [OLD, NEW]
    assert len(cleared(ctx)) == 1


def test_a_ci_non_pass_alone_is_no_failure(tmp_path):
    # A CI report cannot tell a failure from a skip or xfail (#2951).
    ctx = baseline_world(tmp_path, [OLD, NEW])
    orch_facts_promises.observe(ctx, reports([OTHER], base=NEW))
    orch_facts_promises.observe_baselines(ctx)
    assert promise_requests(ctx) == []


def test_the_latest_measurement_of_one_commit_wins_whatever_order_files_are_read(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, NEW])
    capture(ctx, "a-latest-green", NEW, passing=[TEST, OTHER], created="2026-11-06T11:30:00+00:00")
    capture(ctx, "z-earlier-red", NEW, passing=[OTHER], failing=[TEST], created="2026-11-06T11:10:00+00:00")
    orch_facts_promises.observe_baselines(ctx)
    assert promise_requests(ctx) == []
    capture(ctx, "b-latest-red", NEW, passing=[OTHER], failing=[TEST], created="2026-11-06T11:40:00+00:00")
    orch_facts_promises.observe_baselines(ctx)
    assert [row["promise"]["base_sha"] for row in promise_requests(ctx)] == [NEW]


def test_unused_test_ids_leave_the_shared_table(tmp_path):
    ctx = baseline_world(tmp_path, [OLD, NEW])
    capture(ctx, "a", OLD, passing=[TEST, OTHER])
    orch_facts_promises.observe_baselines(ctx)
    capture(ctx, "a", OLD, passing=[OTHER], created="2026-11-06T11:20:00+00:00")  # rewritten without TEST
    orch_facts_promises.observe_baselines(ctx)
    readings = json.loads((ctx.store.root / "promise-readings.json").read_text())
    assert readings["ids"] == [OTHER]


def _set_state(ctx, number, state, at=NOW):
    from foragent.tracker import Issue

    ctx.store.ingest([Issue(number=number, title="t", body="b", labels=frozenset(), state=state)], at, {})


def test_an_open_issue_seen_before_any_failure_keeps_its_late_open_time_failure_quiet(tmp_path):
    # Review r1 (#2951): the open state is recorded whether or not a failure was read yet.
    ctx = baseline_world(tmp_path, [OLD, NEW])
    capture(ctx, "green", OLD, passing=[TEST, OTHER], created="2026-11-06T10:00:00+00:00")
    _set_state(ctx, 7, "open")
    orch_facts_promises.observe_baselines(ctx)  # seen open; no failure read yet
    _set_state(ctx, 7, "closed")
    orch_facts_promises.observe_baselines(ctx)  # seen closed
    # Measured while open, this capture finishes after the close.
    capture(ctx, "late-open-red", NEW, passing=[OTHER], failing=[TEST], created="2026-11-06T11:00:00+00:00")
    orch_facts_promises.observe_baselines(ctx)
    assert promise_requests(ctx) == []


def test_a_pass_between_two_failures_of_one_commit_is_a_recovery(tmp_path):
    # Review r1 (#2951): measurements of one commit are not folded into one result.
    ctx = baseline_world(tmp_path, [OLD, NEW])
    capture(ctx, "red-1", NEW, passing=[OTHER], failing=[TEST], created="2026-11-06T11:00:00+00:00")
    orch_facts_promises.observe_baselines(ctx)
    assert len(promise_requests(ctx)) == 1
    capture(ctx, "green", NEW, passing=[TEST, OTHER], created="2026-11-06T11:20:00+00:00")
    capture(ctx, "red-2", NEW, passing=[OTHER], failing=[TEST], created="2026-11-06T11:40:00+00:00")
    orch_facts_promises.observe_baselines(ctx)
    orch_facts_promises.observe_baselines(ctx)
    assert len(promise_requests(ctx)) == 2 and len(cleared(ctx)) == 1


def test_a_failure_measured_after_the_close_but_before_the_next_evaluation_is_reported(tmp_path):
    # Review r2 (#2951): the boundary is the last time the issue was seen open, not when its close was seen.
    clock = [datetime(2026, 11, 6, 12, 0, tzinfo=UTC)]
    ctx = baseline_world(tmp_path, [OLD, NEW])
    ctx.store = StateStore(tmp_path / "state", lambda: clock[0])
    capture(ctx, "green", OLD, passing=[TEST, OTHER], created="2026-11-06T11:00:00+00:00")
    _set_state(ctx, 7, "open", clock[0])
    orch_facts_promises.observe_baselines(ctx)  # seen open at 12:00
    _set_state(ctx, 7, "closed", clock[0])  # closed at 12:01 ...
    capture(ctx, "red", NEW, passing=[OTHER], failing=[TEST], created="2026-11-06T12:02:00+00:00")
    clock[0] = datetime(2026, 11, 6, 12, 5, tzinfo=UTC)  # ... first evaluated at 12:05
    orch_facts_promises.observe_baselines(ctx)
    assert [row["promise"]["base_sha"] for row in promise_requests(ctx)] == [NEW]


def test_an_older_report_without_start_time_ends_at_a_same_commit_pass_after_it(tmp_path):
    # Review r2 (#2951): a report from before start_at began no later than it existed.
    ctx = baseline_world(tmp_path, [OLD, NEW])
    record = orch_inbox.add_request(ctx.store, "gate", f"#7 {TEST} fails", {"promise": {"test": TEST, "issue": 7, "base_sha": NEW}})
    (ctx.store.root / "promises.json").write_text(json.dumps({"open": {TEST: {
        "issue": 7, "request": record["id"], "since": "2026-11-06T12:00:00+00:00", "base_sha": NEW}}}))
    capture(ctx, "green", NEW, passing=[TEST, OTHER], created="2026-11-06T12:20:00+00:00")
    capture(ctx, "red", NEW, passing=[OTHER], failing=[TEST], created="2026-11-06T12:40:00+00:00")
    orch_facts_promises.observe_baselines(ctx)
    assert len(cleared(ctx)) == 1 and len(promise_requests(ctx)) == 2
