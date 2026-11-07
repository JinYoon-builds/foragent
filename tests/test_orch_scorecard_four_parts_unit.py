"""Value and wasted work in the four-part approval-to-merge split (#2484).

Classifier cases use hand-built records. Execution-path cases run the real merge owner
(single and batch paths) through test_merge_owner_proto's offline world and read the
activity records it writes.
"""

from datetime import UTC, datetime, timedelta

import pytest

from helpers.merge_world import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
from test_merge_owner_batch import batched  # noqa: F401
import test_merge_owner_proto as support
from foragent import merge_owner, orch_scorecard_four as four

T0 = datetime(2026, 11, 7, 9, tzinfo=UTC)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def phase(name, start, end, identity=None):
    return (name, (at(start), at(end)), identity)


def activity(name, start, end, **fields):
    return {"kind": "activity", "issue": 1, "at": at(end).isoformat(),
            "data": {"activity": name, "cause": fields.pop("cause", "initial"),
                     "started_at": at(start).isoformat(), "finished_at": at(end).isoformat(), **fields}}


def review(minute, verdict):
    return {"issue": 1, "verdict": verdict, "at": at(minute).isoformat()}


def parts(phases, records=(), reviews=(), starts=(0,)):
    """The four parts from approval at minute 0 to a merge at the last phase's end."""
    merged = max(b for _, (_, b), _ in phases)
    totals = four.parts(at(0), merged, list(phases), list(records), list(reviews), [at(s) for s in starts], 1)
    assert sum(totals.values()) == pytest.approx((merged - at(0)).total_seconds() / 60)
    return {key: round(value, 6) for key, value in totals.items()}


# ---------------------------------------------------------------- phase fragments are not rounds

@pytest.mark.parametrize("name", ["implementing", "reviewing", "checking"])
@pytest.mark.parametrize("wait", ["executor-wait", "test-slot-wait", "overlap-wait"])
def test_a_wait_inside_a_first_round_keeps_it_value_work(name, wait):
    totals = parts([phase(name, 0, 2), phase(wait, 2, 4), phase(name, 4, 12)])
    assert totals == {"value_work": 10, "wasted_work": 0, "necessary_waiting": 0, "wasted_waiting": 2}


def test_a_wait_inside_a_wasted_round_keeps_it_wasted():
    totals = parts([phase("implementing", 0, 5), phase("reviewing", 5, 7), phase("implementing", 7, 9),
                    phase("executor-wait", 9, 10), phase("implementing", 10, 13)], reviews=[review(7, "FAIL")])
    assert totals["value_work"] == 7 and totals["wasted_work"] == 5 and totals["wasted_waiting"] == 1


def test_a_ci_poll_resumed_after_a_wait_keeps_the_heads_kind():
    phases = [phase("implementing", 0, 5), phase("ci", 5, 7), phase("executor-wait", 7, 8), phase("ci", 8, 10)]
    records = [activity("ci", 5, 7, head="a"), activity("ci", 8, 10, head="a")]
    assert parts(phases, records)["value_work"] == 9


def test_a_recorded_work_identity_decides_the_round():
    same = parts([phase("checking", 0, 2, "1:w:x"), phase("ci", 2, 3), phase("checking", 3, 5, "1:w:x")])
    other = parts([phase("checking", 0, 2, "1:w:x"), phase("checking", 3, 5, "1:w:y")])
    assert same["value_work"] == 5  # Both checking spans and the first CI.
    assert other == {"value_work": 2, "wasted_work": 2, "necessary_waiting": 0, "wasted_waiting": 1}


@pytest.mark.parametrize("separator", ["fail", "ci-failure", "owner-ci-fail", "new-run"])
def test_a_failure_or_new_execution_ends_the_round(separator):
    phases = [phase("implementing", 0, 5), phase("executor-wait", 5, 6), phase("implementing", 6, 9)]
    records, reviews, starts = [], [], [0]
    if separator == "fail":
        reviews = [review(5.5, "FAIL")]
    elif separator == "ci-failure":
        records = [{"kind": "ci-failure", "issue": 1, "at": at(5.5).isoformat(), "data": {}}]
    elif separator == "owner-ci-fail":
        records = [activity("ci", 5.2, 5.5, head="a", result="fail")]
    else:
        starts = [0, 6]
    totals = parts(phases, records, reviews, starts)
    assert totals["wasted_work"] == 3


def test_an_instant_work_phase_still_separates_rounds():
    totals = parts([phase("implementing", 0, 5), phase("checking", 5, 5), phase("implementing", 5, 8)])
    assert totals == {"value_work": 5, "wasted_work": 3, "necessary_waiting": 0, "wasted_waiting": 0}


# ---------------------------------------------------------------- CI and refresh kinds

@pytest.mark.parametrize("head, cause, value, wasted", [
    ("a", "base-changed", 7, 3), ("b", "base-changed", 10, 0), ("a", "group-failed", 7, 3),
])
def test_ci_on_a_known_head_after_a_base_move_is_wasted(head, cause, value, wasted):
    phases = [phase("implementing", 0, 5), phase("ci", 5, 7), phase("ci", 7, 10)]
    records = [activity("ci", 5, 7, head="a"), activity("ci", 7, 10, head=head, cause=cause)]
    totals = parts(phases, records)
    assert (totals["value_work"], totals["wasted_work"]) == (value, wasted)


@pytest.mark.parametrize("neutral, value, wasted", [(False, 9, 0), (True, 6, 3)])
def test_a_base_refresh_is_value_only_when_it_integrates_a_real_change(neutral, value, wasted):
    # The runner refreshes inside its checking phase; the refresh record names the work.
    phases = [phase("implementing", 0, 5), phase("ci", 5, 6), phase("checking", 6, 9)]
    records = [activity("ci", 5, 6, head="a"), activity("base-refresh", 6, 9, cause="base-changed", neutral=neutral)]
    totals = parts(phases, records)
    assert (totals["value_work"], totals["wasted_work"]) == (value, wasted)


def test_records_without_a_head_fall_back_to_what_happened_since_the_last_ci():
    phases = [phase("implementing", 0, 5), phase("ci", 5, 6), phase("reviewing", 6, 7),
              phase("implementing", 7, 8), phase("ci", 8, 9), phase("ci", 9, 10)]
    records = [activity("ci", 9, 10, cause="base-changed")]
    totals = parts(phases, records, reviews=[review(7, "FAIL")])
    # First CI and the CI after the fix build are value; the base-changed rerun is wasted.
    assert totals["value_work"] == 5 + 1 + 1 + 1 and totals["wasted_work"] == 1 + 1


def test_owner_work_wins_over_a_workers_wait_phase():
    # The owner works in its own process; a worker wait recorded at the same time does not hide it.
    phases = [phase("implementing", 0, 5), phase("ci", 5, 6), phase("merge-turn-wait", 6, 20)]
    records = [activity("ci", 5, 6, head="a"),
               activity("base-refresh", 8, 10, cause="base-changed", neutral=False, source="merge-owner"),
               activity("ci", 10, 16, cause="base-changed", head="b", result="pass", source="merge-owner"),
               activity("merging", 18, 20, source="merge-owner")]
    assert parts(phases, records) == {"value_work": 14, "wasted_work": 0, "necessary_waiting": 2,
                                      "wasted_waiting": 4}


@pytest.mark.parametrize("result, used, value, wasted", [("used", 2, 9, 0), ("unused", 0, 5, 4)])
def test_a_train_tip_ci_is_value_when_used_and_wasted_when_retired(result, used, value, wasted):
    phases = [phase("implementing", 0, 5), phase("merge-turn-wait", 5, 10)]
    records = [{"kind": "merge-batch", "issue": None, "at": at(10).isoformat(),
                "data": {"members": [{"n": 2}, {"n": 1}], "result": result, "used_members": used,
                         "ci_minutes": 4, "candidate": "c"}}]
    totals = parts(phases, records)
    assert (totals["value_work"], totals["wasted_work"]) == (value, wasted)


@pytest.mark.parametrize("cause, second_head, value, wasted", [
    ("base-changed", "b", 11, 0), ("group-failed", "b", 11, 0),
    ("base-changed", "a", 7, 4), ("group-failed", "a", 7, 4),
])
def test_one_ci_block_split_by_a_wait_is_one_round(cause, second_head, value, wasted):
    # One runner CI block (one activity record) spans two phase fragments around a wait.
    phases = [phase("implementing", 0, 5), phase("ci", 5, 7), phase("ci", 7, 9), phase("executor-wait", 9, 10),
              phase("ci", 10, 12)]
    records = [activity("ci", 5, 7, head="a"), activity("ci", 7, 12, head=second_head, cause=cause)]
    totals = parts(phases, records)
    assert totals == {"value_work": value, "wasted_work": wasted, "necessary_waiting": 0, "wasted_waiting": 1}


def test_a_wait_inside_a_runner_refresh_stays_waiting():
    phases = [phase("implementing", 0, 5), phase("ci", 5, 6), phase("checking", 6, 7), phase("test-slot-wait", 7, 9),
              phase("checking", 9, 10)]
    records = [activity("ci", 5, 6, head="a"), activity("base-refresh", 6, 10, cause="base-changed", neutral=False)]
    assert parts(phases, records) == {"value_work": 8, "wasted_work": 0, "necessary_waiting": 0, "wasted_waiting": 2}


def test_an_owner_ci_superseded_unread_is_wasted_and_does_not_mark_its_head():
    phases = [phase("implementing", 0, 5), phase("ci", 5, 6)]
    records = [activity("ci", 5, 6, head="a"),
               activity("ci", 6, 9, head="b", cause="base-changed", result="superseded", source="merge-owner"),
               activity("ci", 9, 12, head="c", cause="base-changed", result="pass", source="merge-owner")]
    phases.append(phase("merging", 12, 12))
    assert parts(phases, records) == {"value_work": 9, "wasted_work": 3, "necessary_waiting": 0, "wasted_waiting": 0}


@pytest.mark.parametrize("result, used, value, wasted", [("used", 2, 9, 0), ("unused", 0, 5, 4)])
def test_train_records_without_an_issue_reach_each_member(tmp_path, result, used, value, wasted):
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state", now=lambda: at(10))
    # merge_owner_batch._close writes the train record with no issue: only its members name it.
    store.record_flow("merge-batch", members=[{"n": 2}, {"n": 1}], candidate="c", result=result,
                      used_members=used, reason="r", ci_minutes=4)
    store.record_flow("merge-batch", members=[{"n": 3}], candidate="d", result="used", used_members=1,
                      reason="r", ci_minutes=4)
    records = four.issue_records(store.flow_records(), 1)
    assert [row["data"]["candidate"] for row in records] == ["c"]
    totals = parts([phase("implementing", 0, 5), phase("merging", 10, 10)], records)
    assert (totals["value_work"], totals["wasted_work"]) == (value, wasted)


# ---------------------------------------------------------------- merge owner execution paths

def owner_records(case, n):
    return [row["data"] for row in case.store.flow_records()
            if row["kind"] == "activity" and row["issue"] == n and row["data"].get("source") == "merge-owner"]


def slow(case, monkeypatch, owner, name, minutes):
    original = getattr(owner, name)

    def taking(*args, **kwargs):
        case.clock[0] += timedelta(minutes=minutes)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, taking)


@pytest.mark.parametrize("ci", ["pass", "fail"])
def test_single_owner_records_refresh_the_new_heads_ci_and_merging(proto, monkeypatch, ci):
    case = proto
    support.queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 'new main'\n"})
    slow(case, monkeypatch, merge_owner, "_reverify", 3)
    slow(case, monkeypatch, case.tracker, "merge_pr", 2)
    support.gate(case)  # The owner refreshes onto the moved main and pushes a new head.
    refreshed = case.tracker.pr_head_sha(support.pr_for(case, 5).number)
    [refresh] = [row for row in owner_records(case, 5) if row["activity"] == "base-refresh"]
    assert refresh["neutral"] is False
    case.clock[0] += timedelta(minutes=6)
    if ci == "fail":
        case.tracker.set_pr_checks(support.pr_for(case, 5).number, "fail")
    support.gate(case)
    support.gate(case)
    runs = [row for row in owner_records(case, 5) if row["activity"] == "ci"]
    assert [(row["head"], row["result"], row["cause"]) for row in runs] == [(refreshed, ci, "base-changed")]
    started, finished = (datetime.fromisoformat(runs[0][key]) for key in ("started_at", "finished_at"))
    assert (finished - started) >= timedelta(minutes=6)
    merging = [row for row in owner_records(case, 5) if row["activity"] == "merging"]
    assert len(merging) == (1 if ci == "pass" else 0)
    assert bool(case.tracker.merged_prs) == (ci == "pass")


@pytest.mark.parametrize("ci, recorded", [("fail", "fail"), ("pass", "pass"), ("pending", "superseded")])
def test_single_owner_keeps_the_refreshed_heads_ci_when_main_moves_again(proto, ci, recorded):
    from foragent.orch_scorecard_records import _executions, _issues

    case = proto
    support.queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 'new main'\n"})
    support.gate(case)
    refreshed = case.tracker.pr_head_sha(support.pr_for(case, 5).number)
    case.clock[0] += timedelta(minutes=6)
    case.tracker.set_pr_checks(support.pr_for(case, 5).number, ci)
    case.history.update({"src/neighbor.py": "VALUE = 'newer main'\n"})  # Before the owner reads the result.
    support.gate(case)
    runs = [row for row in owner_records(case, 5) if row["activity"] == "ci" and row["head"] == refreshed]
    assert [row["result"] for row in runs] == [recorded]
    started, finished = (datetime.fromisoformat(runs[0][key]) for key in ("started_at", "finished_at"))
    assert finished - started >= timedelta(minutes=6)
    if ci == "pass":
        support.drain(case, count=1)
        now = case.clock[0] + timedelta(minutes=1)
        executions = [row for row in _executions(case.store, now) if row["number"] == 5]
        [day] = four.windows(case.store, now, 1, _issues(case.store, {}), executions)
        [entry] = day["issues"]
        assert entry["value_work"] >= 6, "The refreshed head's CI is value work on the scorecard"


def test_single_owner_records_ci_once_across_ticks(proto):
    case = proto
    support.queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 'new main'\n"})
    support.gate(case)
    case.tracker.set_pr_checks(support.pr_for(case, 5).number, "pending")
    support.gate(case)
    assert not [row for row in owner_records(case, 5) if row["activity"] == "ci"], "Pending CI has no result yet"
    case.clock[0] += timedelta(minutes=1)
    case.tracker.set_pr_checks(support.pr_for(case, 5).number, "pass")
    support.drain(case, count=1)
    assert len([row for row in owner_records(case, 5) if row["activity"] == "ci"]) == 1


def test_single_owner_without_a_refresh_records_no_ci(proto):
    case = proto
    support.queue_workers(case, [5])
    support.drain(case, count=1)
    assert {row["activity"] for row in owner_records(case, 5)} <= {"merging"}


def test_batch_owner_records_checks_and_landing_for_every_member(batched):
    case = batched
    support.queue_workers(case, [5, 1])

    def pushing():
        case.clock[0] += timedelta(minutes=2)

    case.after_target_push = pushing
    support.gate(case)
    assert len(case.target_pushes) == 1
    for n in (5, 1):
        names = [row["activity"] for row in owner_records(case, n)]
        assert names.count("merging") == 1, names
        [landing] = [row for row in owner_records(case, n) if row["activity"] == "merging"]
        assert datetime.fromisoformat(landing["finished_at"]) - datetime.fromisoformat(landing["started_at"]) \
            >= timedelta(minutes=2)


def refreshed_job(case):
    """After an owner refresh: the admitted place, its job file and the refreshed head."""
    from foragent import run_merge_turn as queue

    support.queue_workers(case, [5])
    case.history.update({"src/neighbor.py": "VALUE = 'new main'\n"})
    support.gate(case)
    pr = support.pr_for(case, 5)
    place = queue.seat(case.ctx, 5, pr.base)
    job = merge_owner._load(case.ctx, place, {"publication": {}})
    assert job.get("previous_head"), "The owner refreshed the head"
    return place, job, pr


def test_a_job_that_ends_before_its_ci_result_records_that_ci_as_superseded(proto):
    case = proto
    place, job, pr = refreshed_job(case)
    case.clock[0] += timedelta(minutes=5)
    case.tracker.set_pr_checks(pr.number, "pending")
    case.tracker.set_pr_head_sha(pr.number, "f" * 40)  # Someone else pushed: the owner holds the job.
    support.gate(case)
    runs = [row for row in owner_records(case, 5) if row["activity"] == "ci"]
    assert [(row["head"], row["result"]) for row in runs] == [(job["head"], "superseded")]


def test_the_shared_final_check_records_a_members_finished_ci(proto, monkeypatch):
    # merge_owner_batch.land checks every train member through _ready; its own CI may have ended.
    from types import SimpleNamespace

    case = proto
    place, job, pr = refreshed_job(case)
    case.clock[0] += timedelta(minutes=5)
    monkeypatch.setattr(merge_owner.run_publish, "_final_merge_inputs", lambda *args, **kwargs: SimpleNamespace(
        status="pass", stop=None, base_sha="other", comparison_current=True, decision=None))
    case.tracker.close_issue(5)
    assert merge_owner._ready(case.ctx, place, job, pr, None, "base", None) is False
    runs = [row for row in owner_records(case, 5) if row["activity"] == "ci"]
    assert [(row["head"], row["result"]) for row in runs] == [(job["head"], "pass")]
