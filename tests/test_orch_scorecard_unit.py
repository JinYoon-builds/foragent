"""Edges of `fa orch scorecard` the acceptance test does not pin."""

import argparse
from datetime import UTC, datetime, timedelta
import json

import pytest

from foragent import orch_scorecard
from foragent.state import StateStore

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.clock = clock
    return store


def finish(store, number, *, at, minutes=10, outcome="merged", detail="", merged_at=None):
    store.clock[0] = at - timedelta(minutes=minutes)
    store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))
    store.clock[0] = at
    if merged_at is not None:
        store.record_merge(number, merged_at)
    store.finish_run(number, outcome=outcome, detail=detail)
    store.clock[0] = NOW


def test_bucket_edges_are_newer_inclusive_and_older_exclusive():
    assert orch_scorecard._bucket(NOW, NOW, 7) == 0
    assert orch_scorecard._bucket(NOW - timedelta(hours=24), NOW, 7) == 1
    assert orch_scorecard._bucket(NOW - timedelta(hours=23, minutes=59), NOW, 7) == 0
    assert orch_scorecard._bucket(NOW - timedelta(days=7), NOW, 7) is None
    assert orch_scorecard._bucket(NOW + timedelta(seconds=1), NOW, 7) is None


def test_pr_open_counts_as_merged_only_after_a_merge_and_otherwise_is_no_failure(store):
    finish(store, 1, at=NOW - timedelta(hours=2), outcome="pr-open", minutes=20,
           merged_at=NOW - timedelta(hours=1))
    finish(store, 2, at=NOW - timedelta(hours=2), outcome="pr-open")
    card = orch_scorecard.collect(store)
    today = card["window"]["today"]
    assert today["runs"] == 2 and today["merge_rate"] == 0.5
    assert today["median_minutes_to_merge"] == 20  # Start to finish, not to merge.
    assert card["unexplained_failures"] == [] and card["repeated_failures"] == []


def test_github_calls_and_review_returns_land_in_their_windows(store):
    for hour in (NOW - timedelta(hours=1), NOW - timedelta(hours=1), NOW - timedelta(hours=30)):
        store.count_github_call(hour)
    with (store.root / "orch-events.jsonl").open("a") as stream:
        for verdict in ("PASS", "FAIL", "UNUSABLE"):
            stream.write(json.dumps({"at": (NOW - timedelta(hours=3)).isoformat(), "issue": 1,
                                     "kind": "review", "verdict": verdict}) + "\n")
    card = orch_scorecard.collect(store, days=2)
    assert card["window"]["today"]["github_calls_per_hour"] == 2 / 24
    assert card["window"]["previous"]["github_calls_per_hour"] == 1 / 24
    assert card["window"]["today"]["review_returns"] == 2
    assert card["window"]["previous"]["review_returns"] == 0
    assert set(card["worse"]) == {"github_calls_per_hour", "review_returns"}


def test_integer_medians_round_half_up(store):
    # Previous buckets hold 1 and 2 runs: median 1.5 rounds to 2.
    finish(store, 1, at=NOW - timedelta(days=1, hours=1))
    finish(store, 2, at=NOW - timedelta(days=2, hours=1))
    finish(store, 3, at=NOW - timedelta(days=2, hours=2))
    assert orch_scorecard.collect(store, days=3)["window"]["previous"]["runs"] == 2


def test_human_output_names_each_worse_metric_and_failures(store):
    finish(store, 1, at=NOW - timedelta(hours=1), outcome="checks-failed", detail="Boom")
    finish(store, 2, at=NOW - timedelta(hours=1), outcome="checks-failed", detail=" Boom ")
    finish(store, 3, at=NOW - timedelta(hours=1), outcome="crashed")
    card = orch_scorecard.collect(store)
    assert card["repeated_failures"] == [{"reason": "Boom", "count": 2, "issues": [1, 2]}]
    assert card["unexplained_failures"] == [3]
    lines = orch_scorecard.human(card, "en").splitlines()
    # Today, its flow line, the previous days, their flow line, the verdict, failures, then main tests.
    assert len(lines) == 8 and "Main tests" in lines[7] and "flow" in lines[1] and "Boom" in lines[5] and "#3" in lines[6]


@pytest.mark.parametrize("value", ["1", "0", "x", "2.5"])
def test_days_below_two_or_not_an_integer_is_a_usage_error(value):
    with pytest.raises(argparse.ArgumentTypeError):
        orch_scorecard._days(value)


def card(worse, today, previous):
    blank = {"phase_minutes": {}}
    return {"worse": worse, "window": {"today": {**blank, **today}, "previous": {**blank, **previous}}}


def test_worst_picks_the_largest_relative_change_with_zero_as_full_change_and_ties_in_order():
    sample = card(["median_minutes_to_merge", "review_returns", "phase_minutes.checking"],
                  {"median_minutes_to_merge": 15.0, "review_returns": 1,
                   "phase_minutes": {"checking": 30.0}},
                  {"median_minutes_to_merge": 10.0, "review_returns": 0,
                   "phase_minutes": {"checking": 20.0}})
    # 50%, 100% (before 0), 50%: the zero baseline wins.
    assert orch_scorecard.worst(sample) == "review_returns"
    tie = card(["median_minutes_to_merge", "phase_minutes.checking"],
               {"median_minutes_to_merge": 20.0, "phase_minutes": {"checking": 4.0}},
               {"median_minutes_to_merge": 10.0, "phase_minutes": {"checking": 2.0}})
    assert orch_scorecard.worst(tie) == "median_minutes_to_merge"
    assert orch_scorecard.worst(card([], {}, {})) is None
    assert orch_scorecard.worst_line(card([], {}, {}), "en") is None


def test_worst_line_uses_registry_labels_and_raw_phase_names():
    phase = card(["phase_minutes.checking"], {"phase_minutes": {"checking": 9.0}},
                 {"phase_minutes": {"checking": 3.0}})
    assert orch_scorecard.worst_line(phase, "en") == "Worse than the days before: checking minutes 3.0 -> 9.0"
    raw = card(["phase_minutes.packing"], {"phase_minutes": {"packing": 9.0}},
               {"phase_minutes": {"packing": 3.0}})
    assert orch_scorecard.worst_line(raw, "en") == "Worse than the days before: packing minutes 3.0 -> 9.0"
    rate = card(["merge_rate"], {"merge_rate": 0.5}, {"merge_rate": 1.0})
    assert orch_scorecard.worst_line(rate, "ko") == "그 전 며칠보다 나빠졌어요: 머지 비율 100.0% -> 50.0%"


@pytest.mark.parametrize("records", ["missing", "non-directory", "empty"])
def test_missing_optional_machine_records_keep_scorecard_consumers_working(store, tmp_path, monkeypatch, records):
    from foragent import capacity
    from foragent.config import Config

    machine = tmp_path / "machine"
    if records == "non-directory":
        machine.write_text("")
        machine /= "foragent"
    elif records == "empty":
        machine.mkdir()
        (machine / capacity.HISTORY_FILE).write_text("")
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    finish(store, 1, at=NOW - timedelta(days=1, hours=1), minutes=10)
    finish(store, 2, at=NOW - timedelta(hours=1), minutes=30)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = orch_scorecard.collect(store, days=2)
    today = result["window"]["today"]
    assert today["idle_minutes"] == 0  # No capacity observation: unknown, never idle.
    assert today["waste_share"] == 0
    assert today["lead_time_minutes"] is None  # No approval record.
    assert today["person_interventions"] == 0
    assert today["wait_minutes_by_cause"] == dict.fromkeys(("slot", "overlap", "merge", "quota"), 0)
    assert orch_scorecard.binding(store, Config(), tmp_path, result) == {"today": "none", "previous": "none"}
    assert orch_scorecard.worst(result) == "median_minutes_to_merge"
    assert orch_scorecard.worst_line(result, "en") == (
        "Worse than the days before: minutes to merge 10.0 -> 30.0")
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_flow_idle_counts_sibling_repositories_on_the_shared_budgets(tmp_path, monkeypatch):
    from foragent import capacity
    from foragent.issue import body_hash

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    start = NOW - timedelta(hours=1)
    body = "<!-- foragent:v1 -->\n## Goal\nG\n## Files\n- a.py\n## Out of scope\nO\n## Done when\n```sh\ntrue\n```\n"
    store.save_approval(1, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=start.isoformat())
    store.update_local_issue(1, title="t", body=body, labels=["approved", "ready"], state="open", blocked_by=[])
    for name in ("parallel", "test_slots"):
        capacity.append_budget_history(tmp_path, start, name, 0, 1, pressure="normal", load_per_cpu=0.1, swapins=0)
    assert orch_scorecard.collect(store, 2)["window"]["today"]["idle_minutes"] == pytest.approx(60)
    clock = [start]
    sibling = StateStore(tmp_path / "other-repo", now=lambda: clock[0])
    sibling.start_run(5, branch="foragent/issue-5", worktree=sibling.worktree_path(5))
    sibling.update_run(5, pid=4242, phase="checking", phase_started_at=start.isoformat())
    assert orch_scorecard.collect(store, 2)["window"]["today"]["idle_minutes"] == 0, (
        "A sibling's live run and its checking phase fill the machine-wide budgets")


def test_flow_waste_counts_ongoing_runtime_and_merged_running_records(store):
    finish(store, 1, at=NOW - timedelta(hours=2), minutes=10, outcome="error")
    store.clock[0] = NOW - timedelta(minutes=90)
    store.start_run(2, branch="foragent/issue-2", worktree=store.worktree_path(2))
    store.clock[0] = NOW
    today = orch_scorecard.collect(store, 2)["window"]["today"]
    assert today["waste_share"] == pytest.approx(0.1), "10 failed of 100 runtime minutes, 90 still running"
    assert today["merges"] == 0
    store.record_merge(2, NOW - timedelta(minutes=5))
    assert orch_scorecard.collect(store, 2)["window"]["today"]["merges"] == 1, (
        "A merged runner still cleaning up has merged")


def test_flow_idle_without_label_events_uses_records_not_current_labels(tmp_path, monkeypatch):
    from foragent import capacity
    from foragent.issue import body_hash

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    approved, started = NOW - timedelta(hours=2), NOW - timedelta(minutes=90)
    capacity.append_budget_history(tmp_path, NOW - timedelta(days=1), "parallel", 0, 1,
                                   pressure="normal", load_per_cpu=0.1, swapins=0)
    body = "<!-- foragent:v1 -->\n## Goal\nG\n## Files\n- a.py\n## Out of scope\nO\n## Done when\n```sh\ntrue\n```\n"
    store.save_approval(1, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=approved.isoformat())
    clock[0] = started
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    clock[0] = NOW - timedelta(minutes=60)
    store.record_merge(1, clock[0])
    store.finish_run(1, outcome="merged")
    # Only `approved` is left now; `ready` was removed when it started (no label history).
    store.update_local_issue(1, title="t", body=body, labels=["approved"], state="closed", blocked_by=[],
                             merged=True, merged_at=clock[0].isoformat())
    clock[0] = NOW
    assert orch_scorecard.collect(store, 2)["window"]["today"]["idle_minutes"] == pytest.approx(30)
    # A hold counts from its flag time, not from the approval.
    store.update_local_issue(2, title="t2", body=body, labels=["approved", "ready", "blocked"], state="open",
                             blocked_by=[])
    store.save_approval(2, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=(NOW - timedelta(minutes=20)).isoformat())
    store.save_flag(2, "stalled", flagged_at=(NOW - timedelta(minutes=10)).isoformat())
    assert orch_scorecard.collect(store, 2)["window"]["today"]["idle_minutes"] == pytest.approx(40)


def test_flow_idle_counts_a_merged_blocker_until_its_merge(tmp_path, monkeypatch):
    from foragent import capacity
    from foragent.issue import body_hash

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    capacity.append_budget_history(tmp_path, NOW - timedelta(days=1), "parallel", 0, 1,
                                   pressure="normal", load_per_cpu=0.1, swapins=0)
    body = "<!-- foragent:v1 -->\n## Goal\nG\n## Files\n- a.py\n## Out of scope\nO\n## Done when\n```sh\ntrue\n```\n"
    store.save_approval(1, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=(NOW - timedelta(hours=1)).isoformat())
    store.update_local_issue(99, title="b", labels=[], state="closed", merged=True,
                             merged_at=(NOW - timedelta(minutes=30)).isoformat())
    store.update_local_issue(1, title="t", body=body, labels=["approved", "ready"], state="open",
                             blocked_by=[{"number": 99, "state": "closed", "repository": None}])
    assert orch_scorecard.collect(store, 2)["window"]["today"]["idle_minutes"] == pytest.approx(30), (
        "The blocker was open until its merge 30 minutes ago")


def _paused_case(tmp_path, monkeypatch):
    """Issue 1 approved two hours ago with a free slot ever since; the clock drives plan edits."""
    from foragent import capacity
    from foragent.issue import body_hash

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    clock = [NOW - timedelta(hours=2)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    capacity.append_budget_history(tmp_path, NOW - timedelta(days=1), "parallel", 0, 1,
                                   pressure="normal", load_per_cpu=0.1, swapins=0)
    body = "<!-- foragent:v1 -->\n## Goal\nG\n## Files\n- a.py\n## Out of scope\nO\n## Done when\n```sh\ntrue\n```\n"
    store.save_approval(1, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=clock[0].isoformat())
    store.update_local_issue(1, title="t", body=body, labels=["approved", "ready"], state="open", blocked_by=[])
    return store, clock


def _idle_now(store, clock):
    clock[0] = NOW
    return orch_scorecard.collect(store, 2)["window"]["today"]["idle_minutes"]


def test_flow_idle_a_later_pause_of_the_lane_replaces_the_earlier_one(tmp_path, monkeypatch):
    from foragent import orch_plan

    store, clock = _paused_case(tmp_path, monkeypatch)
    orch_plan.pause(store, "main", NOW + timedelta(hours=1), "long")  # 10:00 until 13:00
    clock[0] = NOW - timedelta(minutes=90)
    orch_plan.pause(store, "main", NOW - timedelta(minutes=20), "shorter")  # 10:30 until 11:40
    assert _idle_now(store, clock) == pytest.approx(20), "Only 11:40 to 12:00 is unpaused"


def test_flow_idle_a_pause_ends_at_the_recorded_plan_expiry_not_when_noticed(tmp_path, monkeypatch):
    from foragent import orch_plan

    store, clock = _paused_case(tmp_path, monkeypatch)
    orch_plan.pause(store, "main", NOW + timedelta(hours=1), "long")
    orch_plan.set_plan(store, {}, NOW + timedelta(minutes=30))
    clock[0] = NOW - timedelta(minutes=60)
    orch_plan.set_plan(store, {}, NOW - timedelta(minutes=30))  # The later record replaces the earlier expiry.
    assert _idle_now(store, clock) == pytest.approx(30), "The plan expired at 11:30 with no tick watching"
    clock[0] = NOW - timedelta(minutes=10)
    orch_plan.record_expiry(store, clock[0])  # A tick notices it at 11:50.
    assert _idle_now(store, clock) == pytest.approx(30), "The noticing time is not the end"


def test_flow_a_recorded_merge_counts_even_when_a_later_step_failed(store):
    store.save_approval(1, body="b", body_sha="s", acceptance_blobs={}, approved_by="human",
                        approved_at=(NOW - timedelta(hours=2)).isoformat())
    store.update_local_issue(1, title="t", labels=["approved"], state="closed")
    finish(store, 1, at=NOW - timedelta(hours=1), minutes=30, outcome="error",
           merged_at=NOW - timedelta(hours=1, minutes=10))
    today = orch_scorecard.collect(store, 2)["window"]["today"]
    assert today["waste_share"] == 0, "Merged work is not wasted because cleanup failed"
    assert today["merges"] == 1
    assert today["lead_time_minutes"] == pytest.approx(50), "Approval 10:00 to merge 10:50 keeps its sample"


def test_flow_idle_a_current_label_change_never_rewrites_earlier_days(tmp_path, monkeypatch):
    """Without label events, a current hold applies only from the issue's recorded update time."""
    from foragent import capacity
    from foragent.issue import body_hash

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    clock = [NOW - timedelta(days=3)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    capacity.append_budget_history(tmp_path, clock[0], "parallel", 0, 1, pressure="normal",
                                   load_per_cpu=0.1, swapins=0)
    body = "<!-- foragent:v1 -->\n## Goal\nG\n## Files\n- a.py\n## Out of scope\nO\n## Done when\n```sh\ntrue\n```\n"
    store.save_approval(1, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=clock[0].isoformat())
    store.update_local_issue(1, title="t", body=body, labels=["approved", "ready"], state="open", blocked_by=[])

    def idle():
        clock[0] = NOW
        window = orch_scorecard.collect(store, 2)["window"]
        return window["previous"]["idle_minutes"], window["today"]["idle_minutes"]

    assert idle() == (pytest.approx(1440), pytest.approx(1440))
    clock[0] = NOW - timedelta(hours=1)
    store.update_local_issue(1, labels=["ready"])  # Approval removed an hour ago.
    # #827: a removed approval with no label history and no run is never startable in the window.
    assert idle() == (pytest.approx(0), pytest.approx(0)), "A removed approval is not idle work"
    clock[0] = NOW - timedelta(days=3)
    store.update_local_issue(2, title="t2", body=body, labels=["approved", "ready"], state="open", blocked_by=[])
    store.save_approval(2, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=clock[0].isoformat())
    clock[0] = NOW - timedelta(minutes=30)
    store.update_local_issue(2, labels=["approved", "ready", "blocked"])  # No flag record.
    assert idle() == (pytest.approx(1440), pytest.approx(1410)), "The hold counts from its update, not all along"


def test_flow_idle_reads_the_parallel_state_file_from_its_changed_at(tmp_path, monkeypatch):
    from foragent import capacity
    from foragent.issue import body_hash

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    body = "<!-- foragent:v1 -->\n## Goal\nG\n## Files\n- a.py\n## Out of scope\nO\n## Done when\n```sh\ntrue\n```\n"
    store.save_approval(1, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=(NOW - timedelta(hours=2)).isoformat())
    store.update_local_issue(1, title="t", body=body, labels=["approved", "ready"], state="open", blocked_by=[])
    assert orch_scorecard.collect(store, 2)["window"]["today"]["idle_minutes"] == 0, "No budget record: unknown"
    capacity.write_state(store, 1, NOW - timedelta(hours=1))
    assert orch_scorecard.collect(store, 2)["window"]["today"]["idle_minutes"] == pytest.approx(60), (
        "The budget file is a record from its changed_at")


def test_flow_removed_approval_rule_spares_running_and_closed_issues():
    """#827: only an open, not running issue without a current approved label is never startable."""
    from foragent.orch_scorecard_flow import _labels_at

    approved, updated = NOW - timedelta(days=1), NOW - timedelta(minutes=10)
    issue = {"events": [], "approved_at": approved, "labels": {"ready"}, "closed": False,
             "flagged_at": None, "updated_at": updated, "running": False}
    before = NOW - timedelta(hours=1)
    assert _labels_at(issue, before) == set()
    assert _labels_at({**issue, "running": True}, before) == {"approved", "ready"}
    assert _labels_at({**issue, "running": True}, NOW) == {"ready"}
    assert _labels_at({**issue, "closed": True}, before) == {"approved", "ready"}
    assert _labels_at({**issue, "labels": {"approved"}}, NOW) == {"approved", "ready"}


def test_proposal_retry_and_correction_count_only_the_latest_explicit_acceptance(store):
    from types import SimpleNamespace
    from foragent import orch_inbox, orch_roles

    config = SimpleNamespace(roles={"architect": {"agent": "fa-architect"}})
    ordinary = orch_inbox.add_request(store, "planning", "ordinary")
    orch_inbox.set_request_status(store, ordinary["id"], "done", answer="Completed", require_answer=True)
    proposal = orch_inbox.add_request(store, "architect", "proposal")

    def count():
        reopened = StateStore(store.root, now=lambda: store.clock[0])
        card = orch_roles.scorecard(reopened, config, orch_scorecard.collect(reopened, days=2))
        return card["window"]["today"]["roles"]["architect"]["accepted_proposals"]

    assert count() == 0
    for _ in range(2):
        orch_roles.decide(store, proposal["id"], "accepted", "architect", answer="Applied")
        assert count() == 1
    orch_roles.decide(store, proposal["id"], "rejected", None, answer="Reconsidered")
    assert count() == 0
    assert orch_inbox.requests(store)[1]["accepted"] is False


@pytest.mark.parametrize("stage", ["status-event", "proposal-event"])
@pytest.mark.parametrize("decision", ["accepted", "rejected"])
def test_scorecard_uses_committed_decision_when_its_audit_event_fails(store, monkeypatch, stage, decision):
    from types import SimpleNamespace
    from foragent import orch_events, orch_inbox, orch_roles

    config = SimpleNamespace(roles={"architect": {"agent": "fa-architect"}})
    request = orch_inbox.add_request(store, "architect", "proposal")
    previous = "rejected" if decision == "accepted" else "accepted"
    orch_roles.decide(store, request["id"], previous, "architect", answer="Earlier decision")
    store.clock[0] += timedelta(minutes=1)

    def unavailable(*args, **kwargs):
        raise OSError("audit write failed")

    target = orch_inbox if stage == "status-event" else orch_events
    monkeypatch.setattr(target, "append_event", unavailable)
    with pytest.raises(OSError, match="audit write failed"):
        orch_roles.decide(store, request["id"], decision, None, answer="Corrected decision")
    reopened = StateStore(store.root, now=lambda: store.clock[0])
    [row] = orch_inbox.requests(reopened)
    assert row["status"] == "done"
    assert row["answer"] == "Corrected decision"
    assert row["accepted"] is (decision == "accepted")
    assert row["decided_at"] == store.clock[0].isoformat()

    def count():
        card = orch_roles.scorecard(reopened, config, orch_scorecard.collect(reopened, days=2))
        return card["window"]["today"]["roles"]["architect"]["accepted_proposals"]

    assert count() == int(decision == "accepted")
    assert count() == int(decision == "accepted"), "Reading must not duplicate the acceptance"
    (store.root / "orch-events.jsonl").write_text("")
    assert count() == int(decision == "accepted"), "Journal retention must not erase an inbox decision"


def test_scorecard_keeps_the_decision_day_after_a_later_status_note(store):
    from types import SimpleNamespace
    from foragent import orch_inbox, orch_roles

    config = SimpleNamespace(roles={"architect": {"agent": "fa-architect"}})
    store.clock[0] = NOW - timedelta(hours=30)
    request = orch_inbox.add_request(store, "architect", "proposal")
    orch_roles.decide(store, request["id"], "accepted", "architect", answer="Applied")
    store.clock[0] = NOW
    orch_inbox.set_request_status(store, request["id"], "processing", "Follow-up")
    card = orch_roles.scorecard(store, config, orch_scorecard.collect(store, days=2))
    assert card["window"]["today"]["roles"]["architect"]["accepted_proposals"] == 0
    assert card["window"]["previous"]["roles"]["architect"]["accepted_proposals"] == 1
