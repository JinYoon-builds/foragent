"""Fact boundaries and accounting rules of the four-part and five-stage scorecard."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import orch_scorecard, orch_scorecard_four as four, orch_scorecard_lead as lead, order_prepare
from foragent.state import StateStore

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def at(minutes):
    return NOW + timedelta(minutes=minutes)


def activity(name, start, end, **fields):
    return {"kind": "activity", "at": at(end).isoformat(), "issue": 1,
            "data": {"activity": name, "started_at": at(start).isoformat(),
                     "finished_at": at(end).isoformat(), **fields}}


def test_second_reviewing_round_after_a_failure_is_wasted():
    value = four.parts(at(0), at(8), [("reviewing", (at(0), at(5))), ("reviewing", (at(5), at(8)))], [], [],
                       failures=[at(5)])
    assert value == {"value_work": 5, "wasted_work": 3, "necessary_waiting": 0, "wasted_waiting": 0}


@pytest.mark.parametrize("head,cause,value,waste", [
    ("a", "base-changed", 4, 6),
    ("b", "base-changed", 10, 0),
    (None, "base-changed", 4, 6),
    ("a", "group-failed", 4, 6),
    ("b", "group-failed", 10, 0),
])
def test_ci_same_head_is_wasted_and_new_head_is_value(head, cause, value, waste):
    records = [activity("ci", 0, 4, head="a", cause="initial"), activity("ci", 4, 10, head=head, cause=cause)]
    parts = four.parts(at(0), at(10), [("ci", (at(0), at(4))), ("ci", (at(4), at(10)))], records, [])
    assert parts["value_work"] == value
    assert parts["wasted_work"] == waste
    assert sum(parts.values()) == 10


def test_ci_provenance_attaches_to_the_overlapping_phase_span():
    records = [activity("ci", 0, 4, head="a", cause="initial"),
               activity("ci", 5, 9, head="b", cause="base-changed")]
    phases = [("ci", (at(0), at(4))), ("ci", (at(4), at(10)))]
    parts = four.parts(at(0), at(10), phases, records, [])
    assert parts["value_work"] == 10
    assert parts["wasted_work"] == 0


@pytest.mark.parametrize("finished,necessary,wasted", [(2, 0, 10), (15, 10, 0), (7, 5, 5), (None, 10, 0)])
def test_overlap_requires_the_named_peer_execution_to_be_running(finished, necessary, wasted):
    records = [{"kind": "overlap", "at": at(2).isoformat(), "issue": 1,
                "data": {"holds": [{"file": "shared.py", "limit": 1,
                                    "peers": [{"issue": 2, "run_started_at": at(0).isoformat()}]}]}}]
    execution = {"number": 2, "started": at(0), "finished": at(finished) if finished is not None else None}
    parts = four.parts(at(2), at(12), [("overlap-wait", (at(2), at(12)))], records, [execution])
    assert parts["necessary_waiting"] == necessary
    assert parts["wasted_waiting"] == wasted


def test_overlap_uses_hold_changes_and_exact_execution_identity():
    records = [{"kind": "overlap", "at": at(0).isoformat(), "issue": 1,
                "data": {"holds": [{"peers": [{"issue": 2, "run_started_at": at(-10).isoformat()}]}]}},
               {"kind": "overlap", "at": at(5).isoformat(), "issue": 1, "data": {"holds": []}}]
    executions = [{"number": 2, "started": at(-10), "finished": at(2)},
                  {"number": 2, "started": at(2), "finished": None}]
    parts = four.parts(at(0), at(10), [], [*records, activity("overlap-wait", 0, 10, cause="file-overlap")], executions)
    assert parts["necessary_waiting"] == 2
    assert parts["wasted_waiting"] == 8


def test_utilization_uses_the_recorded_lane_budget():
    executions = [{"started": at(-60), "finished": None}]
    one = four.utilization(executions, [(at(-1440), 1)], NOW, 1)[0]
    two = four.utilization(executions, [(at(-1440), 2)], NOW, 1)[0]
    assert one == pytest.approx(60 / 1440)
    assert two == pytest.approx(one / 2)


def test_utilization_integrates_budget_changes_and_never_undercounts_busy_slots():
    executions = [{"started": at(-60), "finished": None}]
    assert four.utilization(executions, [(at(-1440), 1), (at(-720), 2)], NOW, 1) == pytest.approx([60 / 2160])
    assert four.utilization(executions * 2, [(at(-60), 1)], NOW, 1) == [1]
    assert four.utilization(executions, [], NOW, 1) == [1]
    assert four.utilization([], [], NOW, 1) == [None]


def test_activity_fills_only_phase_gaps_and_unrecorded_time_is_wasted_waiting():
    parts = four.parts(at(0), at(20), [("reviewing", (at(5), at(10)))],
                       [activity("implementing", 2, 5), activity("checking", 10, 15)], [], at(10))
    assert parts == {"value_work": 8, "wasted_work": 5, "necessary_waiting": 0, "wasted_waiting": 7}
    assert sum(parts.values()) == 20


def test_first_check_after_pass_and_resolving_conflicts_are_wasted():
    phases = [("checking", (at(0), at(3))), ("resolving-conflict", (at(3), at(7))), ("merging", (at(7), at(10)))]
    parts = four.parts(at(0), at(10), phases, [], [], at(0))
    assert parts == {"value_work": 0, "wasted_work": 7, "necessary_waiting": 3, "wasted_waiting": 0}


def test_only_necessary_waiting_has_an_unknown_efficiency(tmp_path):
    clock = [at(-10)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.save_approval(1, body="b", body_sha="s", acceptance_blobs={}, approved_by="human",
                        approved_at=clock[0].isoformat())
    store.start_run(1, branch="b", worktree="w")
    store.update_run(1, phase="merging", phase_started_at=clock[0].isoformat())
    clock[0] = NOW
    store.finish_run(1, outcome="merged")
    result = orch_scorecard.collect(store, days=2)["window"]["today"]["flow_efficiency"]
    assert result["issues"] == [{"issue": 1, "value_work": 0, "wasted_work": 0,
                                  "necessary_waiting": 10, "wasted_waiting": 0, "efficiency": None}]
    assert result["median"] is None


@pytest.mark.parametrize("heading", ["### D7. Decision (2026-10-24, Jin)", "## 2026-10-24\n### D7. Decision"])
def test_lead_time_dates_the_earliest_linked_decision_from_the_working_tree(tmp_path, heading):
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    (tmp_path / "docs").mkdir()
    ledger = tmp_path / "docs" / "decisions.md"
    ledger.write_text(f"# Decisions\n{heading}\n## 2026-10-25\n### D8. Later\n", encoding="utf-8")
    path = order_prepare._record_path(store, {"id": "order"})
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"issues": {"child": {"attempts": [
        {"role": "writer", "started_at": at(-120).isoformat()},
        {"role": "reviewer", "status": "reviewed", "started_at": at(-100).isoformat(),
         "finished_at": at(-80).isoformat()},
        {"role": "reviewer", "status": "reviewed", "started_at": at(-70).isoformat(),
         "finished_at": at(-60).isoformat()},
    ]}}}), encoding="utf-8")
    issue = {"number": 1, "body": '<!-- foragent:plan-child id=order key=child -->\n'
             '<!-- foragent:decisions ["D8", "D7"] -->', "created_at": at(-40).isoformat()}
    execution = {"started": at(-20), "merged_at": NOW}
    before = {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in (ledger, path)}
    dates = lead.decision_dates(tmp_path)
    result = lead.entry(store, issue, execution, dates)
    decided = datetime(2026, 10, 24, tzinfo=UTC)
    assert result["start"] == "decided"
    assert list(result["stages"]) == list(lead.STAGES)
    assert result["stages"]["decided->ordered"] == (at(-120) - decided).total_seconds() / 60
    assert list(result["stages"].values())[1:] == [60, 20, 20, 20]
    assert result["minutes"] == (NOW - decided).total_seconds() / 60
    undated = lead.entry(store, issue, execution, lead.decision_dates(None))
    assert undated["start"] == "ordered" and undated["stages"][lead.STAGES[0]] is None
    assert {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in before} == before


def test_worst_picks_the_stage_with_the_largest_absolute_increase():
    before = {"medians": dict.fromkeys(lead.STAGES, 20)}
    today = {"medians": {**before["medians"], "ordered->prepared": 50, "prepared->published": 80}}
    names = lead.worse_names(today, before)
    assert names == ["lead_time.prepared->published"]
    card = {"worse": ["review_returns", *names],
            "window": {"today": {"lead_time": today, "review_returns": 2},
                       "previous": {"lead_time": before, "review_returns": 1}}}
    assert orch_scorecard.worst(card) == "lead_time.prepared->published"
    assert "lead time prepared->published 20 -> 80" in orch_scorecard.worst_line(card, "en")
    assert lead.worse_names(before, today) == []


def test_worse_stage_selection_uses_absolute_increase_before_relative_competition():
    before = {"medians": dict.fromkeys(lead.STAGES, None)}
    before["medians"].update({"ordered->prepared": 1, "prepared->published": 20})
    today = {"medians": {**before["medians"], "ordered->prepared": 11, "prepared->published": 80}}
    assert lead.worse_names(today, before) == ["lead_time.prepared->published"]


def test_flow_efficiency_competes_as_a_drop_and_is_shown_as_a_percent():
    card = {"worse": ["review_returns", "flow_efficiency"],
            "window": {"previous": {"review_returns": 10, "flow_efficiency": {"median": 1}},
                       "today": {"review_returns": 11, "flow_efficiency": {"median": .5}}}}
    assert orch_scorecard.worst(card) == "flow_efficiency"
    assert orch_scorecard.worst_line(card, "en") == "Worse than the days before: flow efficiency 100% -> 50.0%"


def test_previous_windows_take_daily_medians_and_skip_unknown_days():
    efficiencies = [{"median": value, "issues": [], "utilization": usage}
                    for value, usage in ((1, .5), (None, None), (.25, 1))]
    assert four.previous(efficiencies) == {"median": .625, "issues": [], "utilization": .75}
    days = [{"medians": dict.fromkeys(lead.STAGES, value), "issues": []} for value in (10, None, 30)]
    assert lead.previous(days)["medians"] == dict.fromkeys(lead.STAGES, 20)


@pytest.mark.parametrize("name", ["implementing", "reviewing", "checking"])
def test_waits_resume_the_same_value_round(name):
    phases = [(name, (at(0), at(2))), ("test-slot-wait", (at(2), at(4))), (name, (at(4), at(12)))]
    result = four.parts(at(0), at(12), phases, [], [], at(3))
    assert result == {"value_work": 10, "wasted_work": 0, "necessary_waiting": 0, "wasted_waiting": 2}


@pytest.mark.parametrize("name", ["implementing", "reviewing", "checking"])
def test_waits_resume_the_same_wasted_round(name):
    other = "checking" if name != "checking" else "implementing"
    phases = [(name, (at(0), at(2))), (other, (at(2), at(2))), (name, (at(2), at(4))),
              ("test-slot-wait", (at(4), at(6))), (name, (at(6), at(10)))]
    assert four.parts(at(0), at(10), phases, [], []) == {
        "value_work": 2, "wasted_work": 6, "necessary_waiting": 0, "wasted_waiting": 2}


@pytest.mark.parametrize("other", ["reviewing", "checking", "ci", "resolving-conflict"])
def test_zero_length_work_events_separate_rounds(other):
    phases = [("implementing", (at(0), at(2))), (other, (at(3), at(3))),
              ("implementing", (at(4), at(12)))]
    assert four.parts(at(0), at(12), phases, [], []) == {
        "value_work": 2, "wasted_work": 8, "necessary_waiting": 0, "wasted_waiting": 2}


@pytest.mark.parametrize("head,cause,separator,expected", [
    ("a", "initial", None, (10, 0)),
    ("a", "base-changed", None, (2, 8)),
    ("a", "group-failed", None, (2, 8)),
    ("b", "base-changed", "checking", (10, 0)),
    (None, "initial", None, (10, 0)),
    (None, "initial", "checking", (2, 8)),
    (None, "initial", "ci-failure", (2, 8)),
    (None, "base-changed", None, (2, 8)),
])
def test_ci_polling_resumes_by_head_and_unknown_heads_require_round_continuity(head, cause, separator, expected):
    phases = [("ci", (at(0), at(2))), ("test-slot-wait", (at(2), at(4))), ("ci", (at(4), at(12)))]
    records = [activity("ci", 0, 2, head=head, cause="initial"),
               activity("ci", 4, 12, head="a" if head == "a" else head, cause=cause)]
    if head == "b":
        records[0]["data"]["head"] = "a"
    if separator == "checking":
        phases.append(("checking", (at(3), at(3))))
    elif separator == "ci-failure":
        records.append({"kind": separator, "at": at(3).isoformat(), "issue": 1, "data": {}})
    result = four.parts(at(0), at(12), phases, records, [])
    assert (result["value_work"], result["wasted_work"]) == expected
    assert result["wasted_waiting"] == 2


def history(tmp_path, phases, *, run_start=0, run_end=12, merged=12, records=(), reviews=()):
    """Read real journal/run/flow records through the public scorecard collector."""
    from foragent import orch_events

    clock = [at(0)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    store.save_approval(1, body="b", body_sha="s", acceptance_blobs={}, approved_by="human",
                        approved_at=clock[0].isoformat())
    clock[0] = at(run_start)
    store.start_run(1, branch="b", worktree="w")
    for name, (start, end) in phases:
        orch_events.append_event(store, at(end), 1, "phase", name,
                                 phase=name, phase_started_at=at(start).isoformat())
    for moment, verdict, issue in reviews:
        orch_events.append_event(store, at(moment), issue, "review", verdict, verdict=verdict)
    for record in records:
        clock[0] = datetime.fromisoformat(record["at"])
        store.record_flow(record["kind"], issue=record.get("issue"),
                          run_started_at=record.get("run_started_at"), **record["data"])
    clock[0] = at(run_end)
    store.record_merge(1, at(merged))
    store.finish_run(1, outcome="pr-open" if run_end < merged else "merged")
    clock[0] = at(merged + 1)
    return store, clock


@pytest.mark.parametrize("failure", ["review", "ci-failure", "other-issue"])
def test_recorded_failures_split_work_rounds_only_for_the_same_issue(tmp_path, failure):
    phases = [("implementing", (0, 2)), ("test-slot-wait", (2, 4)), ("implementing", (4, 12))]
    records = [{"kind": "ci-failure", "at": at(3).isoformat(), "issue": 1, "data": {}}] if failure == "ci-failure" else []
    reviews = [(3, "FAIL", 2 if failure == "other-issue" else 1)] if failure != "ci-failure" else []
    store, _ = history(tmp_path, phases, records=records, reviews=reviews)
    issue = orch_scorecard.collect(store, days=2)["window"]["today"]["flow_efficiency"]["issues"][0]
    assert issue["value_work"] == (10 if failure == "other-issue" else 2)
    assert issue["wasted_work"] == (0 if failure == "other-issue" else 8)
    assert issue["wasted_waiting"] == 2


def test_raw_zero_length_phase_survives_a_shared_start_timestamp(tmp_path):
    store, _ = history(tmp_path, [("implementing", (0, 2)), ("checking", (2, 2)), ("implementing", (2, 12))])
    issue = orch_scorecard.collect(store, days=2)["window"]["today"]["flow_efficiency"]["issues"][0]
    assert issue["value_work"] == 2
    assert issue["wasted_work"] == 10


@pytest.mark.parametrize("wait_phase", [None, "overlap-wait", "test-slot-wait"])
def test_admission_holds_before_run_start_are_necessary_only_while_the_peer_runs(wait_phase):
    records = [{"kind": "overlap", "at": at(0).isoformat(), "issue": 1, "run_started_at": None,
                "data": {"holds": [{"peers": [{"issue": 2, "run_started_at": at(-2).isoformat()}]}]}},
               {"kind": "ready", "at": at(0).isoformat(), "issue": 1, "run_started_at": None,
                "data": {"reason": "file-overlap", "outcome": "waiting"}},
               {"kind": "overlap", "at": at(10).isoformat(), "issue": 1, "data": {"holds": []}},
               {"kind": "ready", "at": at(10).isoformat(), "issue": 1,
                "data": {"reason": None, "outcome": "started"}}]
    executions = [{"number": 2, "started": at(-2), "finished": at(10)},
                  {"number": 1, "started": at(10), "finished": at(15)}]
    phases = [("implementing", (at(10), at(15)))]
    if wait_phase:
        phases.append((wait_phase, (at(0), at(10))))
    assert four.parts(at(0), at(15), phases, records, executions) == {
        "value_work": 5, "wasted_work": 0, "necessary_waiting": 10, "wasted_waiting": 0}


def test_overlap_evidence_cannot_override_observed_work():
    records = [{"kind": "overlap", "at": at(0).isoformat(), "issue": 1,
                "data": {"holds": [{"peers": [{"issue": 2, "run_started_at": at(0).isoformat()}]}]}},
               activity("implementing", 0, 10, cause="file-overlap")]
    executions = [{"number": 2, "started": at(0), "finished": None}]
    assert four.parts(at(0), at(10), [], records, executions)["value_work"] == 10


@pytest.mark.parametrize("end", ["ready-changed", "run-started"])
def test_admission_ready_wait_ends_at_the_next_ready_record_or_run_start(end):
    records = [{"kind": "overlap", "at": at(0).isoformat(), "issue": 1, "run_started_at": None,
                "data": {"holds": [{"peers": [{"issue": 2, "run_started_at": at(-2).isoformat()}]}]}},
               {"kind": "ready", "at": at(0).isoformat(), "issue": 1,
                "data": {"outcome": "waiting", "reason": "file-overlap"}}]
    executions = [{"number": 2, "started": at(-2), "finished": None}]
    if end == "ready-changed":
        records.append({"kind": "ready", "at": at(5).isoformat(), "issue": 1,
                        "data": {"outcome": "waiting", "reason": "parallel-full"}})
    else:
        executions.append({"number": 1, "started": at(5), "finished": None})
    assert four.parts(at(0), at(10), [], records, executions) == {
        "value_work": 0, "wasted_work": 0, "necessary_waiting": 5, "wasted_waiting": 5}


@pytest.mark.parametrize("head,value,waste", [("b", 10, 0), ("a", 4, 6)])
def test_owner_ci_and_merging_after_worker_finish_are_accounted(tmp_path, head, value, waste):
    records = [activity("ci", 0, 4, head="a", cause="initial"),
               activity("ci", 4, 10, head=head, cause="base-changed", source="merge-owner"),
               activity("merging", 10, 12, cause="initial", source="merge-owner")]
    store, _ = history(tmp_path, [("ci", (0, 4))], run_end=4, records=records)
    issue = orch_scorecard.collect(store, days=2)["window"]["today"]["flow_efficiency"]["issues"][0]
    assert {part: issue[part] for part in four.PARTS} == {
        "value_work": value, "wasted_work": waste, "necessary_waiting": 2, "wasted_waiting": 0}


@pytest.mark.parametrize("result,used,index,minutes,value,waste,waiting", [
    ("used", 2, 0, 6, 10, 0, 0),
    ("used", 2, 1, 6, 10, 0, 0),
    ("used", 1, 1, 6, 4, 6, 0),
    ("unused", 0, 0, 6, 4, 6, 0),
    ("unused", 2, 0, 6, 4, 6, 0),
    ("used", 1, 0, 0, 4, 0, 6),
])
def test_retired_batch_ci_is_charged_to_each_member(tmp_path, result, used, index, minutes, value, waste, waiting):
    members = [{"n": 2, "head": "a", "started_at": at(0).isoformat()} for _ in range(2)]
    members[index]["n"] = 1
    records = [{"kind": "merge-batch", "issue": None, "at": at(10).isoformat(),
                "data": {"members": members, "result": result, "used_members": used, "ci_minutes": minutes}}]
    store, _ = history(tmp_path, [("ci", (0, 4)), ("merge-turn-wait", (4, 10))],
                       run_end=4, merged=10, records=records)
    issue = orch_scorecard.collect(store, days=2)["window"]["today"]["flow_efficiency"]["issues"][0]
    assert {part: issue[part] for part in four.PARTS} == {
        "value_work": value, "wasted_work": waste, "necessary_waiting": 0, "wasted_waiting": waiting}


def test_owner_records_a_refreshed_heads_ci_once_across_ticks(tmp_path):
    from foragent import merge_owner

    store = StateStore(tmp_path / "state", now=lambda: at(10))
    ctx = SimpleNamespace(store=store, now=lambda: at(10))
    place = SimpleNamespace(n=1, started_at=at(0).isoformat())
    job = {"previous_head": "a", "head": "b", "ready_at": at(4).timestamp()}
    expected = {"activity": "ci", "cause": "base-changed", "head": "b", "source": "merge-owner",
                "started_at": at(4).isoformat(), "finished_at": at(10).isoformat()}
    assert merge_owner._ci_activity(job, at(10)) == expected
    merge_owner._note_ci(ctx, place, job)
    saved = json.loads(merge_owner._path(ctx, 1).read_text())
    merge_owner._note_ci(ctx, place, saved)
    rows = store.flow_records()
    assert len(rows) == 1
    assert (rows[0]["issue"], rows[0]["run_started_at"], rows[0]["data"]) == (1, place.started_at, expected)
    saved.update(previous_head="b", head="c", ready_at=at(8).timestamp())
    merge_owner._note_ci(ctx, place, saved)
    assert [row["data"]["head"] for row in store.flow_records()] == ["b", "c"]


def test_owner_does_not_attribute_the_workers_ci_to_an_unrefreshed_job():
    from foragent import merge_owner

    assert merge_owner._ci_activity({"head": "a", "ready_at": at(0).timestamp()}, at(10)) is None


@pytest.mark.parametrize("fails", [False, True])
def test_owner_records_the_merge_interval_even_when_the_merge_fails(tmp_path, fails):
    from foragent import merge_owner

    clock = [at(10)]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    received = []

    def merge(pr, **kwargs):
        received.append((pr, kwargs))
        clock[0] = at(12)
        if fails:
            raise RuntimeError("merge failed")

    ctx = SimpleNamespace(store=store, now=lambda: clock[0], tracker=SimpleNamespace(merge_pr=merge))
    place = SimpleNamespace(n=1, started_at=at(0).isoformat())
    if fails:
        with pytest.raises(RuntimeError, match="merge failed"):
            merge_owner._merge_pr(ctx, place, {"head": "b"}, 9, {"subject": "title"})
    else:
        merge_owner._merge_pr(ctx, place, {"head": "b"}, 9, {"subject": "title"})
    rows = store.flow_records()
    assert len(rows) == 1, "a merge attempt is merging time whether or not it succeeds"
    assert (rows[0]["issue"], rows[0]["run_started_at"], rows[0]["data"]) == (1, place.started_at, {
        "activity": "merging", "cause": "initial", "source": "merge-owner",
        "started_at": at(10).isoformat(), "finished_at": at(12).isoformat()})
    assert received == [(9, {"expected_head_sha": "b", "delete_branch": False, "subject": "title"})]


def test_batch_ci_ends_before_the_owners_checks_and_landing(tmp_path):
    """The merge-batch record is written after landing; tip CI ran before the owner's checks."""
    owner = {"source": "merge-owner", "cause": "initial"}
    members = [{"n": 1, "head": "a", "started_at": at(0).isoformat()}]
    records = [activity("checking", 14, 16, **owner), activity("merging", 16, 18, **owner),
               {"kind": "merge-batch", "issue": None, "at": at(18).isoformat(),
                "data": {"members": members, "result": "used", "used_members": 1, "ci_minutes": 6}}]
    store, _ = history(tmp_path, [("ci", (0, 4))], run_end=4, merged=18, records=records)
    issue = orch_scorecard.collect(store, days=2)["window"]["today"]["flow_efficiency"]["issues"][0]
    # Worker CI 4 + batch tip CI 8..14 = 10 value; owner re-check 2 wasted; landing push 2 necessary.
    assert {part: issue[part] for part in four.PARTS} == {
        "value_work": 10, "wasted_work": 2, "necessary_waiting": 2, "wasted_waiting": 4}


@pytest.mark.parametrize("broken", ["record", "clock"])
def test_owner_observation_errors_do_not_change_publication(tmp_path, monkeypatch, broken):
    from foragent import merge_owner

    store = StateStore(tmp_path / "state", now=lambda: at(10))
    merged = []
    ctx = SimpleNamespace(store=store, now=lambda: at(10),
                          tracker=SimpleNamespace(merge_pr=lambda *args, **kwargs: merged.append(args[0])))
    place = SimpleNamespace(n=1, started_at=at(0).isoformat())

    def unavailable(*args, **kwargs):
        raise OSError("observation unavailable")

    monkeypatch.setattr(store if broken == "record" else ctx, "record_flow" if broken == "record" else "now", unavailable)
    job = {"previous_head": "a", "head": "b", "ready_at": at(4).timestamp()}
    merge_owner._note_ci(ctx, place, job)
    merge_owner._merge_pr(ctx, place, job, 9, {})
    assert merged == [9]


def decision_history(tmp_path):
    clock = [NOW]
    store = StateStore(tmp_path / "state", now=lambda: clock[0])
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "decisions.md").write_text("## 2026-10-24\n### D7. Measure flow\n", encoding="utf-8")
    path = order_prepare._record_path(store, {"id": "order"})
    path.parent.mkdir(parents=True)
    issues = {}
    for number, offset in ((1, -1440), (2, 0)):
        ordered = at(offset - 120)
        started = at(offset - 60)
        issues[str(number)] = {"attempts": [
            {"role": "writer", "started_at": ordered.isoformat()},
            {"role": "reviewer", "status": "reviewed", "finished_at": at(offset - 100).isoformat()}]}
        body = (f"<!-- foragent:plan-child id=order key={number} -->\n"
                '<!-- foragent:decisions ["D7"] -->')
        store.save_approval(number, body=body, body_sha="s", acceptance_blobs={}, approved_by="human",
                            approved_at=started.isoformat())
        store.update_local_issue(number, body=body, state="closed", labels=["approved"],
                                  created_at=at(offset - 80).isoformat())
        clock[0] = started
        store.start_run(number, branch="b", worktree="w")
        store.update_run(number, phase="implementing", phase_started_at=started.isoformat())
        clock[0] = at(offset - 50)
        store.record_merge(number, clock[0])
        store.finish_run(number, outcome="merged")
    path.write_text(json.dumps({"issues": issues}), encoding="utf-8")
    clock[0] = NOW
    return store


@pytest.mark.parametrize("consumer", ["report", "brief", "report-cli", "brief-cli", "scorecard-cli"])
def test_all_scorecard_consumers_name_a_decided_to_ordered_regression(tmp_path, monkeypatch, capsys, consumer):
    from foragent import brief, brief_report, cli
    from foragent.config import Config

    store = decision_history(tmp_path)
    config = Config(language="en")
    if consumer == "report":
        output = brief_report.scorecard_line(store, "en", config=config, repo_root=tmp_path)
    elif consumer == "brief":
        output = brief.collect(store, config, NOW, root=tmp_path)["scorecard_line"]
    elif consumer == "scorecard-cli":
        args = SimpleNamespace(repo_root=tmp_path, days=7, json=True)
        monkeypatch.setattr(orch_scorecard, "binding", lambda *args: {})
        orch_scorecard.command(args, store, config)
        result = json.loads(capsys.readouterr().out)
        assert result["worse"] == ["lead_time.decided->ordered"]
        output = orch_scorecard.worst_line(result, "en")
    else:
        (tmp_path / ".foragent.yml").write_text("repo: owner/repo\nlanguage: en\n", encoding="utf-8")
        monkeypatch.setattr(cli.StateStore, "for_repo", classmethod(lambda cls, *args, **kwargs: store))
        monkeypatch.setattr(cli.update_check, "cached", lambda config: None)
        monkeypatch.setattr(cli.update_check, "check", lambda config: None)
        monkeypatch.setattr(cli.star, "offer", lambda language: None)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW

        monkeypatch.setattr(cli, "datetime", Clock)
        options = ["--report"] if consumer == "report-cli" else []
        assert cli.main(["brief", *options, "--repo-root", str(tmp_path)]) == 0
        output = capsys.readouterr().out
        if consumer == "report-cli":
            output = output.splitlines()[1]
    assert "lead time decided->ordered 3480.0 -> 4920.0" in output
    # Compatibility for callers without context: the ledger-dependent stage remains unknown.
    assert brief_report.scorecard_line(store, "en") is None


def test_shared_card_preserves_lane_pauses_reverts_and_role_metrics(tmp_path, monkeypatch):
    from foragent import capacity, orch_events
    from foragent.config import Config

    store = StateStore(tmp_path / "state", now=lambda: NOW)
    config = Config(base_branch="trunk", lanes={"api": {}})
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    capacity.append_budget_history(tmp_path, at(-60), "parallel", 0, 1,
                                   pressure="normal", load_per_cpu=.25, swapins=0)
    store.save_approval(1, body="b", body_sha="s", acceptance_blobs={}, approved_by="human", approved_at=at(-30).isoformat())
    store.update_local_issue(1, state="open", labels=["approved", "ready", "lane:api"])
    orch_events.append_event(store, at(-30), None, "plan-pause", "pause api", lane="api", until=at(60).isoformat())

    def reverts(root, branch):
        assert (root, branch) == (tmp_path, "trunk")
        return [at(-10)]

    monkeypatch.setattr(orch_scorecard.reverts, "times", reverts)
    result = orch_scorecard.card(store, config, tmp_path, days=2)
    assert result["window"]["today"]["idle_minutes"] == 0
    assert orch_scorecard.collect(store, days=2)["window"]["today"]["idle_minutes"] == 30
    assert result["window"]["today"]["reverts_after_merge"] == 1
    assert "roles" in result["window"]["today"] and "roles" in result["window"]["previous"]


def test_a_recorded_merging_interval_wins_over_an_inferred_batch_ci_interval(tmp_path):
    """A merging record without the owner's source tag does not move the batch CI end; it still wins."""
    members = [{"n": 1, "head": "a", "started_at": at(0).isoformat()}]
    records = [activity("merging", 16, 18, cause="initial"),
               {"kind": "merge-batch", "issue": None, "at": at(18).isoformat(),
                "data": {"members": members, "result": "used", "used_members": 1, "ci_minutes": 6}}]
    store, _ = history(tmp_path, [("ci", (0, 4))], run_end=4, merged=18, records=records)
    issue = orch_scorecard.collect(store, days=2)["window"]["today"]["flow_efficiency"]["issues"][0]
    # Inferred batch CI 12..18 loses 16..18 to the recorded merge: value 4 + 4, necessary 2.
    assert {part: issue[part] for part in four.PARTS} == {
        "value_work": 8, "wasted_work": 0, "necessary_waiting": 2, "wasted_waiting": 8}
