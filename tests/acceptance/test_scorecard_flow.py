"""Acceptance test for scorecard-flow. Locked by Foragent; do not edit."""

from datetime import UTC, datetime, timedelta
import json
import os
import re

import pytest

from test_local_truth import local_cli, record_gh
from test_orch_scorecard import completed_run, scorecard_case
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
CAUSES = {
    "test-slot-wait": "slot",
    "overlap-wait": "overlap",
    "merge-turn-wait": "merge",
    "merge-lock-wait": "merge",
    "executor-wait": "quota",
    "limit-wait": "quota",
}


@pytest.fixture
def flow(git_history, tmp_path, monkeypatch):
    from foragent import capacity

    case = scorecard_case(git_history, tmp_path, monkeypatch)
    case.clock[0] = NOW
    case.files_root = tmp_path
    # Put both shared budgets beside this test's stores, never in the owner's data.
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.25))
    monkeypatch.setattr(os, "getloadavg", lambda: (0.25, 0.25, 0.25))
    case.gh_calls = record_gh(monkeypatch)
    case.main = local_cli(case, monkeypatch)
    return case


def _files(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def _read(case, capsys, action="scorecard", *, human=False, days=2):
    before = _files(case.files_root)
    argv = ["orch", action] + ([] if human else ["--json"])
    if action == "scorecard":
        argv += ["--days", str(days)]
    code = cli_exit(case.main, argv)
    output = capsys.readouterr()
    assert code == 0, f"fa {' '.join(argv)} exited {code}: {output.err.strip()}"
    assert case.gh_calls == [], "Scorecard and facts must read local records without GitHub calls"
    assert _files(case.files_root) == before, "Reading the scorecard/facts must not change local records"
    return output.out if human else json.loads(output.out)


def _card(case, capsys, *, days=2):
    case.clock[0] = NOW
    return _read(case, capsys, days=days)


def _metric(card, name, window="today"):
    metrics = card["window"][window]
    assert name in metrics, f"{window} must expose {name} in scorecard windows"
    return metrics[name]


def _event(case, at, kind, issue=None, **fields):
    from foragent.orch_events import append_event

    return append_event(case.store, at, issue, kind, f"Recorded {kind}", **fields)


def _labels(case, number, events):
    # The gate persists tracker label events in reads.label_events.value.
    case.store.record_read(number, "label_events", [
        {"label": label, "action": action, "actor": "human", "created_at": at.isoformat()}
        for at, label, action in events
    ], baseline=case.store.read_baseline())


def _approved(case, number, at, *, labels=("approved", "ready"), state="open", snapshot=True):
    from foragent.issue import body_hash

    case.clock[0] = at
    body = ("<!-- foragent:v1 -->\n## Goal\nChange the module.\n## Files\n"
            f"- src/issue_{number}.py\n## Out of scope\nOther modules.\n"
            "## Done when\n```sh\ntrue\n```\n")
    if snapshot:
        case.store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                                 approved_by="human", approved_at=at.isoformat())
    case.store.update_local_issue(number, title=f"Issue {number}", body=body,
                                  labels=list(labels), state=state, blocked_by=[])
    _labels(case, number, [(at, label, "labeled") for label in labels])


def _budget(case, at, *, parallel=1, slots=1):
    from foragent import capacity

    old_parallel = capacity.read_state(case.store)
    old_slots = capacity.read_test_slots()
    capacity.write_state(case.store, parallel, at)
    case.store._write_json(capacity.machine_root() / capacity.TEST_SLOTS_FILE,
                           {"slots": slots, "changed_at": at.isoformat()})
    for name, before, after in (("parallel", (old_parallel or {}).get("parallel", 0), parallel),
                                ("test_slots", (old_slots or {}).get("slots", 0), slots)):
        capacity.append_budget_history(capacity.machine_root(), at, name, before, after,
                                       pressure="normal", load_per_cpu=0.25, swapins=0)


def _phased(case, number, started, phases, *, finish=True, outcome="merged"):
    case.clock[0] = started
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    for phase, minutes in phases:
        case.store.update_run(number, phase=phase, phase_started_at=case.clock[0].isoformat())
        case.clock[0] += timedelta(minutes=minutes)
    if finish:
        case.store.update_run(number, last_progress_at=case.clock[0].isoformat())
        case.store.finish_run(number, outcome=outcome)


def _merge(case, number, merged, *, runtime=10, finish_delay=0, outcome="merged"):
    case.clock[0] = merged - timedelta(minutes=runtime)
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
    case.clock[0] = merged
    case.store.record_merge(number, merged)
    case.clock[0] += timedelta(minutes=finish_delay)
    case.store.update_run(number, last_progress_at=case.clock[0].isoformat())
    case.store.finish_run(number, outcome=outcome)
    case.store.update_local_issue(number, state="closed", labels=["approved"], merged=True,
                                  merged_at=merged.isoformat())


@pytest.mark.parametrize("resource", ["parallel", "test_slots"])
def test_idle_minutes_counts_twenty_minutes_of_unused_budget(flow, capsys, resource):
    start = NOW - timedelta(hours=1)
    _approved(flow, 10, start)
    _budget(flow, start)
    # One occupied run/test slot throughout the hour; another slot exists only
    # in the last 20 minutes. Empty capacity before approval is not idle time.
    _phased(flow, 20, start, [("checking", 60)])
    _budget(flow, NOW - timedelta(minutes=20),
            parallel=2 if resource == "parallel" else 1,
            slots=2 if resource == "test_slots" else 1)
    assert _metric(_card(flow, capsys), "idle_minutes") == pytest.approx(20), (
        "Count elapsed minutes with startable approved work and an unused run or test slot")


@pytest.mark.parametrize("condition", ["unapproved", "not-ready", "open-blocker", "blocked", "paused", "closed", "needs-human"])
def test_idle_minutes_excludes_work_that_cannot_start(flow, capsys, condition):
    start = NOW - timedelta(hours=1)
    labels = {"approved", "ready"}
    if condition == "unapproved":
        labels.remove("approved")
    if condition == "not-ready":
        labels.remove("ready")
    if condition in {"blocked", "needs-human"}:
        labels.add(condition)
    _approved(flow, 10, start, labels=sorted(labels), state="closed" if condition == "closed" else "open")
    if condition == "open-blocker":
        flow.store.update_local_issue(99, state="open", labels=[])
        flow.store.update_local_issue(10, blocked_by=[{"number": 99, "state": "open", "repository": None}])
    if condition == "paused":
        from foragent import orch_plan

        flow.clock[0] = start
        orch_plan.pause(flow.store, "main", NOW + timedelta(hours=1), "Person paused this lane")
    _budget(flow, start, parallel=3, slots=3)
    assert _metric(_card(flow, capsys), "idle_minutes") == 0, (
        f"Unused capacity is not idle when the only work is {condition}")


def test_idle_minutes_uses_wall_time_and_ignores_closed_blockers(flow, capsys):
    start = NOW - timedelta(minutes=20)
    _budget(flow, start, parallel=4, slots=4)
    for number in (10, 11):
        _approved(flow, number, start)
        flow.store.update_local_issue(number, blocked_by=[{"number": 99, "state": "closed", "repository": None}])
    flow.store.update_local_issue(99, state="closed", labels=[])
    assert _metric(_card(flow, capsys), "idle_minutes") == pytest.approx(20), (
        "Two eligible issues and four free slots still mean 20 wall minutes, not issue/slot minutes")


def test_idle_minutes_respects_the_prepared_run_start_reservation(flow, capsys):
    start = NOW - timedelta(hours=1)
    _budget(flow, start)
    _approved(flow, 10, start)
    _phased(flow, 20, start, [("checking", 40)])
    flow.clock[0] = NOW - timedelta(minutes=5)
    flow.store.prepare_run_start(10)
    _merge(flow, 10, NOW, runtime=4)
    # The run-start journal reserves the slot one minute before the Run exists.
    _labels(flow, 10, [(start, "approved", "labeled"), (start, "ready", "labeled"),
                       (NOW - timedelta(minutes=5), "ready", "unlabeled"),
                       (NOW - timedelta(minutes=5), "running", "labeled")])
    assert _metric(_card(flow, capsys), "idle_minutes") == pytest.approx(15), (
        "The free interval ends at prepared_at, before the runner writes started_at")


def test_idle_minutes_previous_is_the_median_of_daily_intervals(flow, capsys):
    _budget(flow, NOW - timedelta(days=4))
    for day, idle in ((0, 20), (1, 5), (2, 10), (3, 35)):
        merged = NOW - timedelta(days=day, hours=2)
        approved = merged - timedelta(minutes=30 + idle)
        number = 10 + day
        _approved(flow, number, approved)
        _merge(flow, number, merged, runtime=30)
        _labels(flow, number, [(approved, "approved", "labeled"), (approved, "ready", "labeled"),
                               (merged - timedelta(minutes=30), "ready", "unlabeled"),
                               (merged - timedelta(minutes=30), "running", "labeled")])
    card = _card(flow, capsys, days=4)
    assert _metric(card, "idle_minutes") == pytest.approx(20)
    assert _metric(card, "idle_minutes", "previous") == pytest.approx(10), "Use median([5, 10, 35]), not total idle time"
    assert _metric(_card(flow, capsys, days=3), "idle_minutes", "previous") == pytest.approx(7.5)


def test_empty_windows_distinguish_no_runtime_from_zero_waste(flow, capsys):
    card = _card(flow, capsys)
    for window in ("today", "previous"):
        assert _metric(card, "waste_share", window) is None, "No runtime has an unknown waste share"
        assert _metric(card, "lead_time_minutes", window) is None
        assert _metric(card, "idle_minutes", window) == 0
        assert _metric(card, "person_interventions", window) == 0
        waits = _metric(card, "wait_minutes_by_cause", window)
        assert all(waits.get(cause, 0) == 0 for cause in set(CAUSES.values()))
    assert card["worse"] == []
    completed_run(flow, 10, finished=NOW - timedelta(hours=1), minutes=5)
    assert _metric(_card(flow, capsys), "waste_share") == 0, "Measured runtime that all merged has zero waste"


def test_waste_share_weights_runtime_and_keeps_failed_retry_cost(flow, capsys):
    # Failed attempt 30 + unmerged PR 20; merged retry 10 + merged PR 40.
    # A merged execution's wait is not discarded work under this spec.
    completed_run(flow, 10, finished=NOW - timedelta(hours=4), minutes=30, outcome="checks-failed")
    completed_run(flow, 10, finished=NOW - timedelta(hours=3), minutes=10)
    completed_run(flow, 11, finished=NOW - timedelta(hours=2), minutes=20, outcome="pr-open")
    _merge(flow, 12, NOW - timedelta(hours=1), runtime=40, outcome="pr-open")
    _event(flow, NOW - timedelta(hours=1), "phase", issue=12, phase="test-slot-wait", seconds=600,
           phase_started_at=(NOW - timedelta(hours=1, minutes=10)).isoformat())
    completed_run(flow, 90, finished=NOW - timedelta(days=8), minutes=500, outcome="error")
    completed_run(flow, 91, finished=NOW + timedelta(hours=2), minutes=30, outcome="error")
    share = _metric(_card(flow, capsys), "waste_share")
    assert share == pytest.approx(0.5), "Discarded runtime is 50/100, not failed-run count or wait/repeat time"
    assert 0 <= share <= 1


def test_waste_share_clips_runtime_to_the_window(flow, capsys):
    edge = NOW - timedelta(days=1)
    completed_run(flow, 10, finished=edge + timedelta(minutes=10), minutes=30, outcome="error")
    completed_run(flow, 11, finished=NOW - timedelta(hours=2), minutes=30)
    card = _card(flow, capsys)
    assert _metric(card, "waste_share") == pytest.approx(0.25), "Only 10 failed minutes overlap today's 40 runtime minutes"
    assert _metric(card, "waste_share", "previous") == 1


@pytest.mark.parametrize("phase,cause", list(CAUSES.items()), ids=list(CAUSES))
def test_wait_minutes_sums_each_cause_and_deduplicates_phase_replays(flow, capsys, phase, cause):
    _phased(flow, 10, NOW - timedelta(hours=2), [(phase, 2), ("checking", 30), (phase, 5)])
    _phased(flow, 11, NOW - timedelta(days=1, hours=2), [(phase, 3)])
    _phased(flow, 90, NOW - timedelta(days=8), [(phase, 90)])
    _phased(flow, 91, NOW + timedelta(hours=1), [(phase, 90)])
    journal = flow.store.root / "orch-events.jsonl"
    replay = next(json.loads(line) for line in journal.read_text().splitlines()
                  if json.loads(line).get("issue") == 10 and json.loads(line).get("phase") == phase)
    with journal.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(replay) + "\n")
    card = _card(flow, capsys)
    waits = _metric(card, "wait_minutes_by_cause")
    assert waits.get(cause) == pytest.approx(7), "Sum the two unique wait intervals (2 + 5), not their median"
    assert all(waits.get(other, 0) == 0 for other in set(CAUSES.values()) - {cause})
    assert _metric(card, "wait_minutes_by_cause", "previous").get(cause) == pytest.approx(3)
    assert f"wait_minutes_by_cause.{cause}" in card["worse"]


def test_wait_minutes_clips_closed_and_open_phases_at_day_boundaries(flow, capsys):
    edge = NOW - timedelta(days=1)
    _phased(flow, 10, edge - timedelta(minutes=10), [("test-slot-wait", 30)])
    _phased(flow, 11, NOW - timedelta(minutes=7), [("overlap-wait", 7)], finish=False)
    card = _card(flow, capsys)
    waits = _metric(card, "wait_minutes_by_cause")
    assert waits.get("slot") == pytest.approx(20), "Only 20 of the closed phase's 30 minutes fall today"
    assert waits.get("overlap") == pytest.approx(7), "An unfinished wait accrues through now"
    assert _metric(card, "wait_minutes_by_cause", "previous").get("slot") == pytest.approx(10)


def test_wait_minutes_combines_distinct_phases_with_the_same_cause(flow, capsys):
    _phased(flow, 10, NOW - timedelta(hours=3), [
        ("test-slot-wait", 2), ("overlap-wait", 3), ("merge-turn-wait", 5),
        ("merge-lock-wait", 7), ("executor-wait", 11), ("limit-wait", 13), ("checking", 50),
    ])
    waits = _metric(_card(flow, capsys), "wait_minutes_by_cause")
    assert {cause: waits.get(cause) for cause in set(CAUSES.values())} == {
        "slot": 2, "overlap": 3, "merge": 12, "quota": 24,
    }, "Both merge wait phases and both quota wait phases contribute to their shared cause"


@pytest.mark.parametrize("source", ["approved_at", "label-events"])
def test_lead_time_uses_latest_approval_and_actual_merge_time(flow, capsys, source):
    for number, lead in ((10, 30), (11, 90), (12, 150)):
        merged = NOW - timedelta(hours=2)
        approval = merged - timedelta(minutes=lead)
        _approved(flow, number, approval, snapshot=source == "approved_at")
        _merge(flow, number, merged, finish_delay=20)
        # Old approval, revocation, latest approval: lead starts at the latest one.
        _labels(flow, number, [(approval - timedelta(hours=6), "approved", "labeled"),
                               (approval - timedelta(minutes=1), "approved", "unlabeled"),
                               (approval, "approved", "labeled")])
    _approved(flow, 20, NOW - timedelta(hours=10))
    completed_run(flow, 20, finished=NOW - timedelta(hours=1), outcome="checks-failed")
    completed_run(flow, 21, finished=NOW - timedelta(hours=1))  # Missing approval is unknown, not zero.
    assert _metric(_card(flow, capsys), "lead_time_minutes") == pytest.approx(90), (
        "Median approval-to-merge is 90; run duration is 10 and completion is 20 minutes after merge")


def test_lead_time_assigns_issues_by_merge_time_not_run_finish(flow, capsys):
    _approved(flow, 10, NOW - timedelta(hours=25))
    _merge(flow, 10, NOW - timedelta(hours=23), finish_delay=60)
    _approved(flow, 11, NOW - timedelta(hours=25, minutes=30))
    _merge(flow, 11, NOW - timedelta(hours=25), finish_delay=120)
    card = _card(flow, capsys)
    assert _metric(card, "lead_time_minutes") == pytest.approx(120), "Today's merge has 120 minutes of lead time"
    assert _metric(card, "lead_time_minutes", "previous") == pytest.approx(30), (
        "A previous-day merge remains there even when its runner finished today")


def test_person_interventions_counts_decisions_once_even_after_answer(flow, capsys):
    from foragent import orch_inbox

    flow.clock[0] = NOW - timedelta(hours=2)
    orch_inbox.write_plan_heartbeat(flow.store)  # Suppress delivery; no real notification.
    for index in range(2):
        assert cli_exit(flow.main, ["orch", "ask", "--question", f"Choose direction {index}",
                                   "--option", "A", "--option", "B", "--recommend", "1"]) == 0
        capsys.readouterr()
    orch_inbox.answer(flow.store, "q1", "A")
    for kind in ("escalate", "escalate", "person-found", "answer", "request", "review", "retry"):
        _event(flow, flow.clock[0], kind)
    for day, count in ((1, 1), (2, 3), (3, 9), (8, 100)):
        for _ in range(count):
            _event(flow, NOW - timedelta(days=day, hours=2), "person-found")
    _event(flow, NOW + timedelta(minutes=1), "escalate")
    card = _card(flow, capsys, days=4)
    assert _metric(card, "person_interventions") == 5, "Two questions + two escalations + one person-found = five decisions"
    assert _metric(card, "person_interventions", "previous") == 3, "Use median([1, 3, 9]), not a sum or pooled event count"
    assert _metric(_card(flow, capsys, days=3), "person_interventions", "previous") == 2


def test_previous_windows_use_daily_flow_medians_and_honor_days(flow, capsys):
    # Before adding wait runs: waste [.1, .5, .9] and lead [10, 30, 90]; the last day has
    # many runs, so pooling runtime or issues would yield different baselines.
    for day, count, failed, lead in ((0, 2, 1, 60), (1, 10, 1, 10), (2, 2, 1, 30), (3, 30, 27, 90)):
        ended = NOW - timedelta(days=day, hours=2)
        for index in range(count):
            number = 1000 + day * 100 + index
            _approved(flow, number, ended - timedelta(minutes=lead))
            completed_run(flow, number, finished=ended, minutes=10,
                          outcome="checks-failed" if index < failed else "merged")
        _phased(flow, 2000 + day, ended, [("test-slot-wait", (12, 2, 6, 20)[day])], outcome="killed")
    # Killed wait runs contribute discarded runtime and no lead-time sample.
    # Daily shares are 12/102, 16/26 and 290/320: their median is 16/26.
    card = _card(flow, capsys, days=4)
    assert _metric(card, "lead_time_minutes", "previous") == pytest.approx(30), "Baseline is the median of daily medians"
    assert _metric(card, "waste_share", "previous") == pytest.approx(16 / 26)
    assert _metric(card, "wait_minutes_by_cause", "previous").get("slot") == pytest.approx(6)
    short = _card(flow, capsys, days=3)
    assert _metric(short, "lead_time_minutes", "previous") == pytest.approx(20)
    assert _metric(short, "waste_share", "previous") == pytest.approx(((12 / 102) + (16 / 26)) / 2)
    assert _metric(short, "wait_minutes_by_cause", "previous").get("slot") == pytest.approx(4)


@pytest.mark.parametrize("metric", ["idle_minutes", "waste_share", "lead_time_minutes", "person_interventions",
                                  "wait_minutes_by_cause.slot", "wait_minutes_by_cause.overlap",
                                  "wait_minutes_by_cause.merge", "wait_minutes_by_cause.quota"])
def test_worse_flags_each_flow_metric_only_when_higher(flow, capsys, metric):
    for day, value in ((3, 20), (2, 20), (1, 5), (0, 20)):
        ended = NOW - timedelta(days=day, hours=2)
        number = 10 + day
        if metric == "idle_minutes":
            approved = ended - timedelta(minutes=30 + value)
            _approved(flow, number, approved)
            _merge(flow, number, ended, runtime=30)
            _labels(flow, number, [(approved, "approved", "labeled"), (approved, "ready", "labeled"),
                                   (ended - timedelta(minutes=30), "ready", "unlabeled"),
                                   (ended - timedelta(minutes=30), "running", "labeled")])
        elif metric == "waste_share":
            completed_run(flow, number, finished=ended, minutes=value, outcome="error")
            completed_run(flow, number + 100, finished=ended, minutes=30 - value)
        elif metric == "lead_time_minutes":
            _approved(flow, number, ended - timedelta(minutes=value))
            _merge(flow, number, ended, runtime=1)
        elif metric == "person_interventions":
            for _ in range(value):
                _event(flow, ended, "person-found")
        else:
            phase = next(phase for phase, cause in CAUSES.items() if metric.endswith("." + cause))
            _phased(flow, number, ended, [(phase, value)])
    _budget(flow, NOW - timedelta(days=4))
    card = _card(flow, capsys)
    assert metric in card["worse"], f"worse must include {metric} when today's value is higher"
    # With the same persisted records, earlier clocks compare 5 < 20 and 20 == 20.
    for days_back in (1, 2):
        flow.clock[0] = NOW - timedelta(days=days_back)
        after = _read(flow, capsys)
        name, _, cause = metric.partition(".")
        today = _metric(after, name)
        previous = _metric(after, name, "previous")
        if cause:
            today, previous = today.get(cause, 0), previous.get(cause, 0)
        assert today is not None and previous is not None, "Improvement/equality must compare measured values"
        assert today <= previous
        assert metric not in after["worse"], f"worse must omit {metric} when it improves or stays equal"
    assert "merges" not in card["worse"]


def test_merges_remain_context_and_never_a_worse_target(flow, capsys):
    for day, count in ((0, 1), (1, 3)):
        for index in range(count):
            completed_run(flow, 10 + day * 10 + index, finished=NOW - timedelta(days=day, hours=2))
    card = _card(flow, capsys)
    assert _metric(card, "merges") == 1, "Keep today's merge count as context"
    assert _metric(card, "merges", "previous") == 3
    assert "merges" not in card["worse"], "Fewer merges alone must never be a regression target"


@pytest.mark.parametrize("changed", [False, True], ids=["same-constraint", "changed-constraint"])
def test_human_output_names_facts_binding_and_its_previous_day_change(flow, capsys, changed):
    _budget(flow, NOW - timedelta(days=2), parallel=4)
    before_phase = "overlap-wait" if changed else "test-slot-wait"
    _phased(flow, 10, NOW - timedelta(days=1, minutes=10), [(before_phase, 10)], finish=False)
    previous = _read(flow, capsys, "facts")["binding"]
    assert previous["kind"] == ("structure" if changed else "test_slots")
    flow.store.finish_run(10, outcome="killed")
    _phased(flow, 11, NOW - timedelta(minutes=10), [("test-slot-wait", 10)], finish=False)
    today = _read(flow, capsys, "facts")["binding"]
    assert today["kind"] == "test_slots"
    shown = _read(flow, capsys, human=True).lower()
    assert "binding" in shown and re.search(r"test[ _-]*slots?", shown), (
        "Human scorecard must name today's binding constraint from fa orch facts: test_slots")
    if changed:
        assert re.search(r"structure|overlap", shown) and re.search(r"changed|previous|yesterday|from", shown), (
            "Human scorecard must say the binding changed from yesterday's structure/overlap to test slots")
    else:
        assert not re.search(r"binding[^\n]*(?:changed from|was .*now)", shown)


def test_note_person_found_appends_one_event_without_requiring_now(flow, capsys):
    flow.clock[0] = NOW
    _event(flow, NOW - timedelta(minutes=1), "review")
    journal = flow.store.root / "orch-events.jsonl"
    before = journal.read_bytes()
    message = "Jin found the stalled queue before the retrospective"
    code = cli_exit(flow.main, ["orch", "note", "--person-found", message])
    output = capsys.readouterr()
    assert code == 0, f"fa orch note --person-found must exit 0 without --now; got {code}: {output.err.strip()}"
    assert journal.read_bytes().startswith(before), "Append the event without replacing existing history"
    added = [json.loads(line) for line in journal.read_bytes()[len(before):].decode().splitlines()]
    assert len(added) == 1 and added[0].get("kind") == "person-found", "One invocation appends exactly one person-found event"
    assert added[0]["at"] == NOW.isoformat() and message in json.dumps(added[0], ensure_ascii=False)
    assert _metric(_card(flow, capsys), "person_interventions") == 1
    assert flow.gh_calls == []
