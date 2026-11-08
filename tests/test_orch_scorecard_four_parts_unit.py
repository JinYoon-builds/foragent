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
from test_run_group_ci import ci_case  # noqa: F401
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
    # Both checking spans; a ci phase with no CI run record is no CI work (CI comes only from run records).
    assert same == {"value_work": 4, "wasted_work": 0, "necessary_waiting": 0, "wasted_waiting": 1}
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
    records = [activity("ci", 5, 6), activity("ci", 8, 9), activity("ci", 9, 10, cause="base-changed")]
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


def train_ci(start, end, head="c"):
    """The tip CI run record merge_owner.ci_result appends: no issue, the members it covers."""
    record = activity("ci", start, end, head=head, cause="batch", result="pass", source="merge-owner",
                      issues=[{"n": 2, "started_at": "x"}, {"n": 1, "started_at": "y"}])
    return {**record, "issue": None}


@pytest.mark.parametrize("result, used, value, wasted", [("used", 2, 9, 0), ("unused", 0, 5, 4)])
def test_a_train_tip_ci_is_value_when_used_and_wasted_when_retired(result, used, value, wasted):
    phases = [phase("implementing", 0, 5), phase("merge-turn-wait", 5, 10)]
    records = [train_ci(6, 10), {"kind": "merge-batch", "issue": None, "at": at(12).isoformat(),
                                 "data": {"members": [{"n": 2}, {"n": 1}], "result": result,
                                          "used_members": used, "candidate": "c"}}]
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
    # The tip CI run record and the train record carry no issue: only the issues they cover name it.
    for head, members in (("c", [2, 1]), ("d", [3])):
        store.record_flow("activity", activity="ci", head=head, cause="batch", result="pass", source="merge-owner",
                          issues=[{"n": n, "started_at": "s"} for n in members],
                          started_at=at(6).isoformat(), finished_at=at(10).isoformat())
        store.record_flow("merge-batch", members=[{"n": n} for n in members], candidate=head, result=result,
                          used_members=used, reason="r", ci_minutes=4)
    records = four.issue_records(store.flow_records(), 1)
    assert [(row["kind"], row["data"]["head" if row["kind"] == "activity" else "candidate"]) for row in records] \
        == [("activity", "c"), ("merge-batch", "c")]
    totals = parts([phase("implementing", 0, 5), phase("merging", 10, 10)], records)
    assert (totals["value_work"], totals["wasted_work"]) == (value, wasted)


# ---------------------------------------------------------------- merge owner execution paths

def owner_records(case, n):
    return [row["data"] for row in four.issue_records(case.store.flow_records(), n)
            if row["kind"] == "activity" and row["data"].get("source") == "merge-owner"]


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


def tip_ci_takes(case, monkeypatch, minutes, status=None):
    """The first poll of a train tip's checks takes ``minutes``; ``status`` overrides its result."""
    from foragent import merge_owner_batch as batch

    checks, seen = case.tracker.pr_checks, set()

    def poll(pr):
        if case.tracker.get_pr(pr).head.startswith(batch.PREFIX):
            if pr not in seen:
                seen.add(pr)
                case.clock[0] += timedelta(minutes=minutes)
            if status is not None:
                return status
        return checks(pr)

    monkeypatch.setattr(case.tracker, "pr_checks", poll)


def scorecard_parts(case, n):
    from foragent.orch_scorecard_records import _executions, _issues

    now = case.clock[0] + timedelta(minutes=1)
    executions = [row for row in _executions(case.store, now) if row["number"] == n]
    [day] = four.windows(case.store, now, 1, _issues(case.store, {}), executions)
    return {entry["issue"]: entry for entry in day["issues"]}


def test_a_used_train_counts_its_tip_ci_and_landing_for_every_member(batched, monkeypatch):
    case = batched
    support.queue_workers(case, [5, 1])
    tip_ci_takes(case, monkeypatch, 4)
    case.after_target_push = lambda: case.clock.__setitem__(0, case.clock[0] + timedelta(minutes=2))
    support.gate(case)
    assert len(case.target_pushes) == 1
    for n in (5, 1):
        [ci] = [row for row in owner_records(case, n) if row["activity"] == "ci"]
        assert ci["result"] == "pass" and {item["n"] for item in ci["issues"]} == {5, 1}
        entry = scorecard_parts(case, n)[n]
        assert entry["value_work"] >= 4, entry
        assert entry["necessary_waiting"] == pytest.approx(2), entry


def test_a_retired_train_counts_its_tip_ci_as_wasted_for_every_member(batched, monkeypatch):
    case = batched
    support.queue_workers(case, [5, 1])
    tip_ci_takes(case, monkeypatch, 4, status="fail")
    support.gate(case)
    assert not case.target_pushes
    for n in (5, 1):
        [ci] = [row for row in owner_records(case, n) if row["activity"] == "ci"]
        assert ci["result"] == "fail" and {item["n"] for item in ci["issues"]} == {5, 1}
        start, end = (datetime.fromisoformat(ci[key]) for key in ("started_at", "finished_at"))
        assert end - start >= timedelta(minutes=4)
        assert four._train(case.store.flow_records(), ci["head"], n) == "retired"


def test_a_train_retired_before_its_tip_ci_reports_records_that_ci_as_superseded(batched, monkeypatch):
    from dataclasses import replace

    case = batched
    support.queue_workers(case, [5, 1])
    tip_ci_takes(case, monkeypatch, 3, status="pending")
    support.gate(case)
    assert not [row for row in owner_records(case, 5) if row["activity"] == "ci"], "Pending: no result yet"
    case.config = replace(case.config, merge_batch=False)  # Retirement: "merge trains disabled".
    case.ctx.config = case.config
    support.gate(case)
    for n in (5, 1):
        [ci] = [row for row in owner_records(case, n) if row["activity"] == "ci"]
        assert ci["result"] == "superseded" and {item["n"] for item in ci["issues"]} == {5, 1}


@pytest.mark.parametrize("verdict, result, part", [
    ("passed", "pass", "value_work"), ("failed", "fail", "value_work"), ("void", "void", "wasted_work"),
])
def test_a_group_leaders_joint_ci_is_work_for_every_member(ci_case, monkeypatch, verdict, result, part):
    # merge_owner off, checks=ci: the leader (#5) measures the group's combined commit once.
    from foragent import ci_mode, run_baseline, run_group_check, run_merge_turn
    from test_run_group_check import NOW

    c = ci_case
    if verdict == "failed":
        c.reports[0]["report"]["head"]["passing_ids"] = []
    elif verdict == "void":
        c.reports[0]["report"]["schema_version"] = 99
    reports = c.ctx.tracker.pr_acceptance_reports

    def slow(head):
        c.clock[0] = max(c.clock[0], NOW + timedelta(minutes=4))  # The joint CI takes 4 minutes.
        return reports(head)

    c.ctx.tracker.pr_acceptance_reports = slow
    c.ctx.tracker.find_pr = lambda branch: int(branch.rsplit("-", 1)[1])  # Each member's own PR.
    monkeypatch.setattr(ci_mode, "enabled", lambda *args: True)
    monkeypatch.setattr(run_baseline, "_baseline_exclusions", lambda *args: [])
    monkeypatch.setattr(run_group_check, "_combine", lambda *args, **kwargs: "head")  # The reports' head.
    monkeypatch.setattr(run_group_check, "_trees", lambda *args: ["tree-5", "tree-1", "tree-4"])
    run_group_check.check(5, c.ctx, run_merge_turn.seat(c.ctx, 5, "main"), "h5", "base", set())
    for n in (5, 1, 4):
        records = four.issue_records(c.ctx.store.flow_records(), n)
        [ci] = [row["data"] for row in records if row["kind"] == "activity" and row["data"]["activity"] == "ci"]
        assert (ci["head"], ci["result"]) == ("head", result)
        assert [item["n"] for item in ci["issues"]] == [5, 1, 4]
        # The leader is in its ci phase; a member waits for its merge turn, and the joint CI is still its work.
        phases = [("ci" if n == 5 else "merge-turn-wait", (NOW, NOW + timedelta(minutes=4)), None)]
        totals = four.parts(NOW, NOW + timedelta(minutes=4), phases, records, [], [NOW], n)
        assert totals[part] == pytest.approx(4), (n, totals)


def test_a_local_group_check_is_recorded_for_every_member(tmp_path, monkeypatch):
    # checks=local: the leader (#5) runs every member's checks on the combined commit.
    from contextlib import nullcontext
    from dataclasses import replace
    from types import SimpleNamespace

    from foragent import ci_mode, run_baseline, run_checks, run_group_check as group, run_implementation, runner
    from foragent import run_merge_turn as turn
    from foragent.run_baseline import AcceptanceBaseline
    from foragent.run_checks import CheckResult
    from test_run_group_check import NOW, make_ctx

    ctx = make_ctx(tmp_path, (5, 1))
    ctx.config = replace(ctx.config, test_command="pytest -q")
    ctx.repo_root = tmp_path
    clock = [NOW]
    ctx.now = lambda: clock[0]
    ctx.monotonic = lambda: (clock[0] - NOW).total_seconds()
    monkeypatch.setattr(runner, "_remaining", lambda *args: 5000)
    monkeypatch.setattr(run_checks, "_related_unit_tests", lambda *args, **kwargs: [])
    monkeypatch.setattr(ci_mode, "enabled", lambda ctx: False)
    monkeypatch.setattr(run_implementation, "_detect_test_command_at", lambda git, sha: None)
    monkeypatch.setattr(run_baseline, "_capture_acceptance_baseline", lambda *args: AcceptanceBaseline(
        "pytest -q", ("tests/acceptance/test_a.py::t",), duration_seconds=60))
    monkeypatch.setattr(group, "_combine", lambda *args, **kwargs: "combined")
    monkeypatch.setattr(group, "_trees", lambda *args: ["tree-5", "tree-1"])
    monkeypatch.setattr(group, "_test_slot", lambda n, ctx: nullcontext())

    def check(command, worktree, log, limit, **kwargs):
        clock[0] += timedelta(minutes=1)  # Each joint command takes a minute.
        return CheckResult(command, 0, False, "")

    monkeypatch.setattr(runner, "_check", check)
    ctx.git = SimpleNamespace(add_detached_worktree=lambda path, ref: None, remove_worktree=lambda path: None,
                              _call=lambda args: "")
    group.check(5, ctx, turn.seat(ctx, 5, "main"), "h5", "main-sha", set())
    end = clock[0]
    assert end - NOW == timedelta(minutes=3)
    for n in (5, 1):
        records = four.issue_records(ctx.store.flow_records(), n)
        [run] = [row["data"] for row in records if row["kind"] == "activity" and row["data"]["cause"] == "group"]
        assert run["activity"] == "checking" and [item["n"] for item in run["issues"]] == [5, 1]
    # Member #1 waits for its turn meanwhile: the joint re-check after its review is its wasted work.
    phases = [("merge-turn-wait", (NOW, end), None)]
    totals = four.parts(NOW, end, phases, four.issue_records(ctx.store.flow_records(), 1),
                        [{"issue": 1, "verdict": "PASS", "at": NOW.isoformat()}], [NOW], 1)
    assert totals["wasted_work"] == pytest.approx(3), totals


@pytest.mark.parametrize("earlier, value, wasted", [((), 8, 0), (("b",), 5, 3)])
def test_a_comparison_wait_takes_the_kind_of_its_heads_latest_ci(earlier, value, wasted):
    # Head b's CI and the wait for its reports carry cause base-changed: value when b is new,
    # wasted when b was already tested and this run only followed a neutral base move.
    phases = [phase("implementing", 0, 4 if earlier else 5)]
    records = [activity("ci", 4, 5, head=head) for head in earlier]
    records += [activity("ci", 5, 6, head="b", cause="base-changed"),
                activity("ci", 6, 8, head="b", cause="base-changed", stage="comparison")]
    totals = parts(phases + [phase("merging", 8, 8)], records)
    assert (totals["value_work"], totals["wasted_work"]) == (value, wasted)
