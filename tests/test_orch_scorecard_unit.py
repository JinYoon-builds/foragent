"""Edges of `fa orch scorecard` the acceptance test does not pin."""

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
import shutil
import subprocess
import threading
import time

import pytest

from conftest import git_command
from foragent import orch_scorecard, orch_scorecard_reverts
from foragent.git import GitRepo
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


@pytest.mark.parametrize("sibling", [False, True])
def test_open_idle_phase_releases_historical_and_current_occupancy(store, sibling):
    import os

    from foragent.orch_scorecard_records import _executions, _occupancy, _phase_intervals

    source = StateStore(store.root.parent / "sibling", now=store._now) if sibling else store
    start, wait = NOW - timedelta(minutes=30), NOW - timedelta(minutes=20)
    store.clock[0] = start
    source.start_run(1, branch="b", worktree="w", pid=os.getpid())
    source.update_run(1, phase="checking", phase_started_at=start.isoformat())
    store.clock[0] = wait
    source.update_run(1, phase="ci", phase_started_at=wait.isoformat())
    store.clock[0] = NOW
    executions, phases = _executions(store, NOW), _phase_intervals(store, NOW)
    occupied, testing = _occupancy(store, NOW, executions, phases, open_end=NOW + timedelta(days=1))
    assert [span for _, span in occupied] == [(start, wait)]
    assert testing == [(start, wait)]
    assert not any(a <= NOW < b for _, (a, b) in occupied)
    assert source.get_run(1).phase == "ci" and len(source.active_runs()) == 1


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


def test_waiting_run_keeps_its_project_place_without_becoming_ready_work(store, monkeypatch, tmp_path):
    import os

    from foragent import capacity
    from foragent.config import Config
    from foragent.issue import body_hash
    from foragent.orch_scorecard_flow import binding_at

    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    start = NOW - timedelta(hours=1)
    body = "<!-- foragent:v1 -->\n## Goal\nG\n## Files\n- a.py\n## Out of scope\nO\n## Done when\n```sh\ntrue\n```\n"
    for number in (1, 2):
        store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                            approved_by="human", approved_at=start.isoformat())
    store.update_local_issue(1, title="waiter", body=body, labels=["approved", "running"], state="open", blocked_by=[])
    store.clock[0] = start
    store.start_run(1, branch="b", worktree="w", pid=os.getpid())
    store.update_run(1, phase="ci", phase_started_at=start.isoformat())
    capacity.write_state(store, 2, start)
    store.clock[0] = NOW
    assert orch_scorecard.collect(store, 2)["window"]["today"]["idle_minutes"] == 0
    store.update_local_issue(2, title="ready", body=body, labels=["approved", "ready"], state="open", blocked_by=[])
    assert binding_at(store, Config(max_parallel=1, overlap_per_file=1), tmp_path, NOW, current=True) == "cap"
    assert binding_at(store, Config(max_parallel=None, overlap_per_file=1), tmp_path, NOW, current=True) == "structure"


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
    # A hold counts from its GitHub marker time, not from approval or a stale local flag.
    store.update_local_issue(2, title="t2", body=body, labels=["approved", "ready", "blocked"], state="open",
                             blocked_by=[])
    store.save_approval(2, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=(NOW - timedelta(minutes=20)).isoformat())
    store.save_flag(2, "stalled", flagged_at=(NOW - timedelta(minutes=15)).isoformat())
    store.record_comments(2, [{"id": 1, "author": "bot",
                              "body": "<!-- foragent:blocked reason=stalled -->\nNo progress",
                              "created_at": (NOW - timedelta(minutes=10)).isoformat()}])
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


def _committed(root, tree, *parents, at, message, monkeypatch):
    """A commit at an exact committer time, built with plumbing (the test guard blocks git commit)."""
    stamp = f"{int(at.timestamp())} +0000"
    for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{key}_NAME", "Offline Tests")
        monkeypatch.setenv(f"{key}_EMAIL", "tests@example.com")
        monkeypatch.setenv(f"{key}_DATE", stamp)
    options = [item for parent in parents for item in ("-p", parent)]
    return git_command(root, "commit-tree", tree, *options, "-m", message)


@pytest.fixture
def repo(git_history):
    git_command(git_history.clone, "fetch", "origin")
    return git_history.clone


def _publish(root, sha, branch="main"):
    git_command(root, "update-ref", f"refs/remotes/origin/{branch}", sha)


SHA = "abc1234def"


@pytest.mark.parametrize("message", [
    f'Revert "Add x"\n\nThis reverts commit {SHA}.',
    f'Revert "Add x" (#12)\n\nWhy it went back.\n\nThis reverts commit {SHA}.',
    f'Reapply "Add x"\n\nThis reverts commit {SHA}.',
    f'Revert "Merge x"\n\nThis reverts commit {SHA}, reversing\nchanges made to {SHA}.',
])
def test_canonical_revert_messages_are_found_at_their_committer_time(repo, monkeypatch, message):
    at = NOW - timedelta(hours=3)
    tip = git_command(repo, "rev-parse", "refs/remotes/origin/main")
    tree = git_command(repo, "rev-parse", f"{tip}^{{tree}}")
    _publish(repo, _committed(repo, tree, tip, at=at, message=message, monkeypatch=monkeypatch))
    assert orch_scorecard_reverts.times(repo, "main") == [at]


@pytest.mark.parametrize("message", [
    f"Squash of the feature\n\nRevert \"Inner\"\n\nThis reverts commit {SHA}.",
    f"Fix the thing\n\nThis reverts commit {SHA}.",
    'Revert "Add x"\n\nNo commit named here.',
    f'Revert "Add x"\n\nMentions: This reverts commit {SHA}.',
    f'Revert "Add x"\n\nThis reverts commit nothex.',
    'Revert "Add x"\n\nThis reverts commit abc123.',
    f'Revert "Add x"\n\nThis reverts commit {SHA}xyz.',
    "Revert the idea of doing x",
])
def test_other_commits_are_not_reverts(repo, monkeypatch, message):
    tip = git_command(repo, "rev-parse", "refs/remotes/origin/main")
    tree = git_command(repo, "rev-parse", f"{tip}^{{tree}}")
    _publish(repo, _committed(repo, tree, tip, at=NOW, message=message, monkeypatch=monkeypatch))
    assert orch_scorecard_reverts.times(repo, "main") == []


def test_a_revert_merged_from_a_side_branch_counts_even_behind_a_newer_merge_time(repo, monkeypatch):
    tip = git_command(repo, "rev-parse", "refs/remotes/origin/main")
    tree = git_command(repo, "rev-parse", f"{tip}^{{tree}}")
    message = f'Revert "Add x"\n\nThis reverts commit {SHA}.'
    # The side parent is older than an unrelated main commit: no first-parent or date walk finds it.
    reverted = NOW - timedelta(days=9)
    side = _committed(repo, tree, tip, at=reverted, message=message, monkeypatch=monkeypatch)
    main_commit = _committed(repo, tree, tip, at=NOW - timedelta(days=1), message="Other", monkeypatch=monkeypatch)
    merge = _committed(repo, tree, main_commit, side, at=NOW, message="Merge side", monkeypatch=monkeypatch)
    _publish(repo, merge)
    assert orch_scorecard_reverts.times(repo, "main") == [reverted]


def test_only_the_configured_default_branch_is_read(repo, monkeypatch):
    tip = git_command(repo, "rev-parse", "refs/remotes/origin/main")
    tree = git_command(repo, "rev-parse", f"{tip}^{{tree}}")
    message = f'Revert "Add x"\n\nThis reverts commit {SHA}.'
    other = _committed(repo, tree, tip, at=NOW, message=message, monkeypatch=monkeypatch)
    _publish(repo, other, "other")
    git_command(repo, "update-ref", "refs/heads/main", other)  # A local main never stands in for origin's.
    git_command(repo, "update-ref", "refs/heads/trunk", other)
    assert orch_scorecard_reverts.times(repo, "main") == []
    assert orch_scorecard_reverts.times(repo, "other") == [NOW]
    assert orch_scorecard_reverts.times(repo, "trunk") is None  # Only a local branch: no origin ref.


def test_shallow_history_is_unknown_not_zero(repo):
    tip = git_command(repo, "rev-parse", "refs/remotes/origin/main")
    assert orch_scorecard_reverts.times(repo, "main") == []
    (repo / ".git" / "shallow").write_text(f"{tip}\n")
    assert orch_scorecard_reverts.times(repo, "main") is None


@pytest.mark.parametrize("error", ["timeout", "command", "os"])
def test_git_failures_are_unknown(repo, monkeypatch, error):
    def fail(args):
        if args[0] == "rev-parse":
            return "false\n"
        raise {"timeout": subprocess.TimeoutExpired("git", 120),
               "command": subprocess.CalledProcessError(128, "git", stderr="bad object"),
               "os": OSError("gone")}[error]

    monkeypatch.setattr(orch_scorecard_reverts, "GitRepo", lambda root, run: GitRepo(root, fail))
    assert orch_scorecard_reverts.times(repo, "main") is None


@pytest.mark.parametrize("stamp", ["not-a-time", "9" * 40, ""])
def test_an_unreadable_commit_time_is_unknown(repo, monkeypatch, stamp):
    output = f'{stamp}\nRevert "Add x"\n\nThis reverts commit {SHA}.\n\0'
    monkeypatch.setattr(orch_scorecard_reverts, "GitRepo", lambda root, run: GitRepo(
        root, lambda args: "false\n" if args[0] == "rev-parse" else output))
    assert orch_scorecard_reverts.times(repo, "main") is None


def _git_shim(tmp_path, monkeypatch, body):
    """A `git` first on PATH; ``$REAL_GIT`` is the real one."""
    folder = tmp_path / "shim"
    folder.mkdir()
    shim = folder / "git"
    shim.write_text(f"#!/bin/sh\n{body}\n")
    shim.chmod(0o755)
    monkeypatch.setenv("REAL_GIT", shutil.which("git"))
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")


def test_a_git_that_is_slow_to_exit_is_read_without_polling(repo, tmp_path, monkeypatch):
    # The output is complete and the pipes are closed while the process still runs:
    # waiting with a timeout here is what calls time.sleep, which local fixtures forbid.
    tip = git_command(repo, "rev-parse", "refs/remotes/origin/main")
    tree = git_command(repo, "rev-parse", f"{tip}^{{tree}}")
    at = NOW - timedelta(hours=1)
    message = f'Revert "Add x"\n\nThis reverts commit {SHA}.'
    _publish(repo, _committed(repo, tree, tip, at=at, message=message, monkeypatch=monkeypatch))
    _git_shim(tmp_path, monkeypatch, '"$REAL_GIT" "$@"\ncode=$?\nexec >&- 2>&-\nsleep 0.3\nexit $code')
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("The read must not poll"))
    assert orch_scorecard_reverts.times(repo, "main") == [at]


def test_a_git_past_its_deadline_is_killed_and_unknown(tmp_path, monkeypatch):
    # A stand-in process: the test guard makes real kills no-ops, so a real child would not end.
    (tmp_path / ".git").mkdir()
    killed = threading.Event()

    class Process:
        returncode = None

        def __init__(self, command, **options):
            self.command = command

        def __enter__(self):
            return self

        def __exit__(self, *details):
            return False

        def kill(self):
            killed.set()

        def communicate(self):
            if self.command[1] == "rev-parse":
                self.returncode = 0
                return "false\n", ""
            self.returncode = -9 if killed.wait(5) else 0
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr(orch_scorecard_reverts, "TIMEOUT_SECONDS", 0.05)
    assert orch_scorecard_reverts.times(tmp_path, "main") is None
    assert killed.is_set()


def test_a_failing_git_is_unknown_even_with_output(repo, tmp_path, monkeypatch):
    message = f'Revert "Add x"\n\nThis reverts commit {SHA}.'
    _git_shim(tmp_path, monkeypatch, 'case "$1" in rev-parse) exec "$REAL_GIT" "$@";; esac\n'
              f"printf '1700000000\\n{message}\\0'\nexit 128".replace('"Add x"', '\\"Add x\\"'))
    assert orch_scorecard_reverts.times(repo, "main") is None


def test_no_repository_is_unknown_without_starting_a_process(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A folder outside any repository must not start Git")

    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    (tmp_path / "plain" / "deep").mkdir(parents=True)
    assert orch_scorecard_reverts.times(tmp_path / "plain" / "deep", "main") is None


@pytest.mark.parametrize("place", ["root", "subfolder", "worktree-file", "git-dir-env"])
def test_a_repository_is_still_read_from_every_place_git_finds_it(repo, tmp_path, monkeypatch, place):
    tip = git_command(repo, "rev-parse", "refs/remotes/origin/main")
    tree = git_command(repo, "rev-parse", f"{tip}^{{tree}}")
    at = NOW - timedelta(hours=1)
    message = f'Revert "Add x"\n\nThis reverts commit {SHA}.'
    _publish(repo, _committed(repo, tree, tip, at=at, message=message, monkeypatch=monkeypatch))
    monkeypatch.delenv("GIT_DIR", raising=False)
    root = repo
    if place == "subfolder":
        root = repo / "deep" / "er"
        root.mkdir(parents=True)
    elif place == "worktree-file":
        root = tmp_path / "linked"
        git_command(repo, "worktree", "add", "--detach", str(root), tip)
        assert (root / ".git").is_file()
    elif place == "git-dir-env":
        root = tmp_path / "elsewhere"
        root.mkdir()
        monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    assert orch_scorecard_reverts.times(root, "main") == [at]


def test_reverts_land_in_time_buckets_and_ignore_journal_events(store):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "orch-events.jsonl").write_text(json.dumps(
        {"at": (NOW - timedelta(hours=1)).isoformat(), "kind": "revert", "summary": "x"}) + "\n")
    moments = [NOW, NOW - timedelta(hours=1),  # Today, the newer end included.
               NOW - timedelta(hours=24),  # Exactly 24 h back is the day before.
               NOW - timedelta(days=2, hours=1), NOW - timedelta(days=2, hours=2),
               NOW - timedelta(days=3, hours=1),  # Outside --days 3.
               NOW + timedelta(seconds=1)]  # The future is in no bucket.
    card = orch_scorecard.collect(store, 3, revert_times=moments)
    assert card["window"]["today"]["reverts_after_merge"] == 2
    # Previous days hold 1 and 2: the median 1.5 rounds half up to 2, so today is not worse.
    assert card["window"]["previous"]["reverts_after_merge"] == 2
    assert "reverts_after_merge" not in card["worse"]
    card = orch_scorecard.collect(store, 3, revert_times=moments[:2])
    assert card["window"]["previous"]["reverts_after_merge"] == 0
    assert "reverts_after_merge" in card["worse"]
    zero = orch_scorecard.collect(store, 3, revert_times=[])
    assert zero["window"]["today"]["reverts_after_merge"] == 0


def test_unknown_reverts_are_null_never_worse_and_keep_other_metrics(store):
    finish(store, 1, at=NOW - timedelta(hours=1))
    card = orch_scorecard.collect(store, 3, revert_times=None)
    for metrics in card["window"].values():
        assert metrics["reverts_after_merge"] is None
    assert "reverts_after_merge" not in card["worse"]
    assert orch_scorecard.worst(card) != "reverts_after_merge"
    assert card["window"]["today"]["runs"] == 1
    assert card["window"]["today"]["merge_rate"] == 1


@pytest.mark.parametrize(("language", "unknown", "known"), [
    ("en", "unknown reverts", "1 reverts"), ("ko", "되돌림 알 수 없음", "되돌림 1건"),
])
def test_human_output_says_unknown_on_both_window_lines(store, language, unknown, known):
    lines = orch_scorecard.human(orch_scorecard.collect(store, 2), language).splitlines()
    assert unknown in lines[0] and unknown in lines[2]
    moments = [NOW - timedelta(hours=1), NOW - timedelta(days=1, hours=1)]
    lines = orch_scorecard.human(orch_scorecard.collect(store, 2, revert_times=moments), language).splitlines()
    assert known in lines[0] and known in lines[2] and unknown not in "\n".join(lines)
