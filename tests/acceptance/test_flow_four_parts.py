"""Acceptance tests for flow-four-parts: approval-to-merge time in four parts.

Each merged issue's time from its latest approval to its merge is split into value work,
wasted work, necessary waiting and wasted waiting. Issue flow efficiency is
value / (value + wasted work + wasted waiting); the scorecard reports its median and the
lane-slot utilization, and the /fa report names a worse median on its second line.
Each merged order child also reports its lead time in five stages (decided->ordered,
ordered->prepared, prepared->published, published->started, started->merged) with daily
stage medians, and the report names the stage whose median worsened most.

Synthetic history enters through the public StateStore methods the gate itself uses
(runs and phases, flow records, journal events). Two tests run the real runner with
scripted model replies. The scorecard and the report are read through the CLI.
Expected minutes are hand-calculated.

Contract chosen by the lane (DECIDE, see the notes of this preparation):
* ``window.<today|previous>.flow_efficiency = {"median", "issues", "utilization"}``.
* Each issue entry: ``issue``, ``value_work``, ``wasted_work``, ``necessary_waiting``,
  ``wasted_waiting`` (minutes) and ``efficiency``; efficiencies and the median are fractions.
* A ``ci`` activity record carries ``head``: the commit CI tested.
* ``window.<w>.lead_time = {"medians": {stage: minutes}, "issues": [{"issue", "start",
  "stages", "minutes"}]}`` and top-level ``lead_time_daily`` (index 0 = today), one
  ``{stage: median}`` per day; an issue sits in the day of its merge.
"""

from datetime import timedelta
import re

import pytest

from test_scorecard_flow import NOW, flow, _approved, _budget, _event, _read  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


PARTS = ("value_work", "wasted_work", "necessary_waiting", "wasted_waiting")
HEAD_A = "a" * 40
HEAD_B = "b" * 40


# ---------------------------------------------------------------- helpers

def at(case, moment):
    case.clock[0] = moment


def record(case, kind, number, **data):
    run = case.store.get_run(number)
    case.store.record_flow(kind, issue=number, run_started_at=run.started_at if run else None, **data)


def start(case, number, moment):
    at(case, moment)
    case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))


def phase(case, number, name, minutes, *, cause=None, review=None, round_number=None, **fields):
    """One phase of ``minutes``; ``cause`` writes its activity record, ``review`` a verdict at its end."""
    began = case.clock[0]
    case.store.update_run(number, phase=name, phase_started_at=began.isoformat())
    case.clock[0] = began + timedelta(minutes=minutes)
    if cause is not None and minutes:
        record(case, "activity", number, activity=name, cause=cause, started_at=began.isoformat(),
               finished_at=case.clock[0].isoformat(), **fields)
    if review is not None:
        _event(case, case.clock[0], "review", issue=number, verdict=review,
               findings=0 if review == "PASS" else 1, round=round_number, source="reviewer")


def merge(case, number):
    merged = case.clock[0]
    case.store.record_merge(number, merged)
    case.store.update_run(number, last_progress_at=merged.isoformat())
    case.store.finish_run(number, outcome="merged")
    case.store.update_local_issue(number, state="closed", labels=["approved"], merged=True,
                                  merged_at=merged.isoformat())


def simple(case, number, approved, *, wait=0, work=10):
    """Approved, ``wait`` unexplained minutes with nobody running, then ``work`` minutes of first build."""
    _approved(case, number, approved)
    start(case, number, approved + timedelta(minutes=wait))
    phase(case, number, "implementing", work, cause="initial")
    merge(case, number)


def efficiency(card, window="today"):
    metrics = card["window"][window]
    assert "flow_efficiency" in metrics, (
        f"The scorecard JSON must expose window.{window}.flow_efficiency")
    value = metrics["flow_efficiency"]
    assert isinstance(value, dict) and {"median", "issues"} <= set(value), (
        "flow_efficiency holds the median and the per-issue parts")
    return value


def issue_parts(card, number, window="today"):
    entries = [entry for entry in efficiency(card, window)["issues"] if entry.get("issue") == number]
    assert len(entries) == 1, f"Merged issue #{number} must appear once in flow_efficiency.issues"
    entry = entries[0]
    assert set(PARTS) <= set(entry), f"Each merged issue reports the four parts: {entry}"
    for part in PARTS:
        assert type(entry[part]) in (int, float) and entry[part] >= 0, entry
    return entry


def card(case, capsys, *, human=False, days=2):
    at(case, NOW)
    return _read(case, capsys, human=human, days=days)


# ---------------------------------------------------------------- four parts add up

def test_four_parts_add_up_and_review_rounds_and_rechecks_are_wasted_work(flow, capsys):
    base = NOW - timedelta(hours=2)
    _approved(flow, 41, base)
    # 3 minutes with nobody running: wasted waiting.
    start(flow, 41, base + timedelta(minutes=3))
    phase(flow, 41, "implementing", 10, cause="initial")            # value: first build
    phase(flow, 41, "checking", 4, cause="initial")                 # value: first local check before review
    phase(flow, 41, "reviewing", 5, review="FAIL", round_number=1)  # value: first review
    phase(flow, 41, "implementing", 6, cause="initial")             # wasted: fix build after FAIL
    phase(flow, 41, "reviewing", 3, review="PASS", round_number=2)  # wasted: second review round
    phase(flow, 41, "checking", 2, cause="initial")                 # wasted: re-check after PASS
    phase(flow, 41, "ci", 4, cause="initial", head=HEAD_A)          # value: first CI on the change
    phase(flow, 41, "merging", 2)                                   # necessary waiting
    merge(flow, 41)

    payload = card(flow, capsys)
    entry = issue_parts(payload, 41)
    assert sum(entry[part] for part in PARTS) == pytest.approx(39), (
        "The four parts add up to approval-to-merge time")
    assert {part: entry[part] for part in PARTS} == pytest.approx({
        "value_work": 23, "wasted_work": 11, "necessary_waiting": 2, "wasted_waiting": 3,
    })
    assert entry["efficiency"] == pytest.approx(23 / 37)
    assert efficiency(payload)["median"] == pytest.approx(23 / 37)


def test_a_clean_run_has_no_wasted_work(flow, capsys):
    base = NOW - timedelta(hours=2)
    _approved(flow, 41, base)
    start(flow, 41, base)
    phase(flow, 41, "implementing", 10, cause="initial")
    phase(flow, 41, "checking", 4, cause="initial")
    phase(flow, 41, "reviewing", 5, review="PASS", round_number=1)
    phase(flow, 41, "ci", 4, cause="initial", head=HEAD_A)
    merge(flow, 41)

    entry = issue_parts(card(flow, capsys), 41)
    assert {part: entry[part] for part in PARTS} == pytest.approx({
        "value_work": 23, "wasted_work": 0, "necessary_waiting": 0, "wasted_waiting": 0,
    }), "First build, first check, first review and first CI are all value work"
    assert entry["efficiency"] == pytest.approx(1)


# ---------------------------------------------------------------- CI after a base move

@pytest.mark.parametrize("second_head, value, wasted", [
    pytest.param(HEAD_A, 14, 6, id="neutral-move-same-code-is-wasted"),
    pytest.param(HEAD_B, 20, 0, id="real-change-from-main-is-value"),
])
def test_ci_on_unchanged_code_after_a_neutral_base_move_is_wasted_work(flow, capsys, second_head, value, wasted):
    base = NOW - timedelta(hours=2)
    _approved(flow, 41, base)
    start(flow, 41, base)
    phase(flow, 41, "implementing", 10, cause="initial")
    phase(flow, 41, "ci", 4, cause="initial", head=HEAD_A)  # The first CI on the change: value.
    # Main moved. The runner refreshes the base (an instant here), then CI runs again.
    phase(flow, 41, "checking", 0)
    phase(flow, 41, "ci", 6, cause="base-changed", head=second_head)
    merge(flow, 41)

    entry = issue_parts(card(flow, capsys), 41)
    assert sum(entry[part] for part in PARTS) == pytest.approx(20)
    assert entry["value_work"] == pytest.approx(value)
    assert entry["wasted_work"] == pytest.approx(wasted)
    assert entry["necessary_waiting"] == pytest.approx(0)
    assert entry["wasted_waiting"] == pytest.approx(0)


# ---------------------------------------------------------------- overlap holds

def overlap_case(case, *, holder_running, parallel=1):
    """#42 waits 10 minutes on src/shared.py, held by #41; then 5 build + 3 CI minutes."""
    base = NOW - timedelta(hours=2)
    _budget(case, base - timedelta(minutes=1), parallel=parallel)
    _approved(case, 41, base)
    start(case, 41, base)
    holder = case.store.get_run(41).started_at
    case.store.update_run(41, phase="implementing", phase_started_at=base.isoformat())
    if not holder_running:
        # #41 stops at +3 with its PR open: its claim stays, but nobody is running.
        at(case, base + timedelta(minutes=3))
        record(case, "activity", 41, activity="implementing", cause="initial",
               started_at=base.isoformat(), finished_at=case.clock[0].isoformat())
        case.store.update_run(41, last_progress_at=case.clock[0].isoformat())
        case.store.finish_run(41, outcome="pr-open")
    _approved(case, 42, base + timedelta(minutes=5))
    start(case, 42, base + timedelta(minutes=5))
    waited = case.clock[0]
    case.store.update_run(42, phase="overlap-wait", phase_started_at=waited.isoformat())
    hold = [{"file": "src/shared.py", "limit": 1, "peers": [{"issue": 41, "run_started_at": holder}]}]
    case.store.record_flow_change("overlap", issue=42, run_started_at=case.store.get_run(42).started_at,
                                  holds=hold)
    at(case, base + timedelta(minutes=15))
    if holder_running:
        record(case, "activity", 41, activity="implementing", cause="initial",
               started_at=base.isoformat(), finished_at=case.clock[0].isoformat())
        merge(case, 41)
    case.store.record_flow_change("overlap", issue=42, run_started_at=case.store.get_run(42).started_at,
                                  holds=[])
    record(case, "activity", 42, activity="overlap-wait", cause="file-overlap",
           started_at=waited.isoformat(), finished_at=case.clock[0].isoformat())
    phase(case, 42, "implementing", 5, cause="initial")
    phase(case, 42, "ci", 3, cause="initial", head=HEAD_A)
    merge(case, 42)


def test_waiting_on_a_file_held_by_a_running_issue_is_necessary_waiting(flow, capsys):
    overlap_case(flow, holder_running=True)
    payload = card(flow, capsys)
    entry = issue_parts(payload, 42)
    assert {part: entry[part] for part in PARTS} == pytest.approx({
        "value_work": 8, "wasted_work": 0, "necessary_waiting": 10, "wasted_waiting": 0,
    })
    assert entry["efficiency"] == pytest.approx(1), "Necessary waiting is outside the efficiency ratio"
    holder = issue_parts(payload, 41)
    assert holder["value_work"] == pytest.approx(15)
    assert efficiency(payload)["median"] == pytest.approx(1)


def test_waiting_on_a_file_with_nobody_running_is_wasted_waiting(flow, capsys):
    overlap_case(flow, holder_running=False)
    payload = card(flow, capsys)
    entry = issue_parts(payload, 42)
    assert {part: entry[part] for part in PARTS} == pytest.approx({
        "value_work": 8, "wasted_work": 0, "necessary_waiting": 0, "wasted_waiting": 10,
    })
    assert entry["efficiency"] == pytest.approx(8 / 18)
    assert efficiency(payload)["median"] == pytest.approx(8 / 18)


def test_scorecard_json_shows_lane_slot_utilization(flow, capsys):
    overlap_case(flow, holder_running=True)
    value = efficiency(card(flow, capsys))
    assert "utilization" in value, "window.today.flow_efficiency.utilization is required"
    utilization = value["utilization"]
    assert type(utilization) in (int, float), f"Utilization is a number: {utilization!r}"
    assert 0 < utilization <= 1, (
        f"Busy lane-slot time over available lane-slot time is a fraction above 0 here: {utilization!r}")
    assert "utilization" in efficiency(card(flow, capsys), "previous")


# ---------------------------------------------------------------- median

def test_scorecard_shows_the_median_issue_flow_efficiency(flow, capsys):
    base = NOW - timedelta(hours=5)
    simple(flow, 51, base, wait=0, work=10)                          # 10 / 10 = 1.0
    simple(flow, 52, base + timedelta(minutes=30), wait=5, work=5)   # 5 / 10 = 0.5
    simple(flow, 53, base + timedelta(minutes=60), wait=30, work=10)  # 10 / 40 = 0.25
    payload = card(flow, capsys)
    value = efficiency(payload)
    assert sorted(entry["issue"] for entry in value["issues"]) == [51, 52, 53]
    assert [issue_parts(payload, n)["efficiency"] for n in (51, 52, 53)] == pytest.approx([1, 0.5, 0.25])
    # Mean 0.583 and pooled 25/60 = 0.417 are both wrong: the median of the issues is 0.5.
    assert value["median"] == pytest.approx(0.5)
    text = card(flow, capsys, human=True)
    lines = [line for line in text.splitlines() if re.search(r"flow efficiency", line, re.I)]
    assert lines, f"The human scorecard shows the median flow efficiency:\n{text}"
    assert re.search(r"(?<![\d.])50(?:\.0)?\s*%", lines[0]), lines[0]


def test_previous_window_has_its_own_median(flow, capsys):
    simple(flow, 61, NOW - timedelta(days=1, hours=6), wait=10, work=10)  # 0.5, previous day
    simple(flow, 62, NOW - timedelta(hours=3), wait=0, work=10)           # 1.0, today
    payload = card(flow, capsys)
    assert efficiency(payload)["median"] == pytest.approx(1)
    assert efficiency(payload, "previous")["median"] == pytest.approx(0.5)
    assert [entry["issue"] for entry in efficiency(payload)["issues"]] == [62]


# ---------------------------------------------------------------- /fa report

def report(case, capsys):
    at(case, NOW)
    capsys.readouterr()
    code = cli_exit(case.main, ["brief", "--report", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, f"fa brief --report exited {code}: {output.err.strip()}"
    assert case.gh_calls == [], "The report reads local records only"
    return output.out.splitlines()


def test_report_second_line_names_a_worse_median_flow_efficiency(flow, capsys):
    # Same 10 minutes approval to merge on both days; only the composition changed.
    simple(flow, 71, NOW - timedelta(days=1, hours=6), wait=0, work=10)  # previous: 100%
    simple(flow, 72, NOW - timedelta(hours=3), wait=5, work=5)           # today: 50%
    lines = report(flow, capsys)
    assert len(lines) >= 3, (
        "With no judgment, a worse median flow efficiency is the report's second line:\n" + "\n".join(lines))
    second = lines[1]
    assert re.search(r"flow efficiency", second, re.I), (
        f"The /fa report's second line names the worse median flow efficiency: {second!r}")
    assert re.search(r"(?:100(?:\.0)?\s*%|1(?:\.0+)?)\s*(?:→|->)\s*(?:50(?:\.0)?\s*%|0?\.50*)", second), (
        f"The line shows the previous and today's median: {second!r}")


def test_report_does_not_name_flow_efficiency_when_it_improves(flow, capsys):
    simple(flow, 71, NOW - timedelta(days=1, hours=6), wait=5, work=5)   # previous: 50%
    simple(flow, 72, NOW - timedelta(hours=3), wait=0, work=10)          # today: 100%
    payload = card(flow, capsys, days=7)
    assert efficiency(payload)["median"] == pytest.approx(1)
    assert efficiency(payload, "previous")["median"] == pytest.approx(0.5)
    lines = report(flow, capsys)
    assert not any(re.search(r"flow efficiency", line, re.I) for line in lines), "\n".join(lines)


# ---------------------------------------------------------------- the real runner

@pytest.fixture
def runner_case(git_history, tmp_path, monkeypatch):
    import os

    from foragent import capacity
    from test_local_truth import local_cli, record_gh
    from test_orch_scorecard import scorecard_case

    case = scorecard_case(git_history, tmp_path, monkeypatch)
    case.files_root = tmp_path
    monkeypatch.setattr(capacity, "machine_root", lambda: tmp_path)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.25))
    monkeypatch.setattr(os, "getloadavg", lambda: (0.25, 0.25, 0.25))
    case.monkeypatch = monkeypatch
    case.record_gh, case.local_cli = record_gh, local_cli
    return case


def runner_card(case, capsys):
    if not hasattr(case, "main"):
        case.gh_calls = case.record_gh(case.monkeypatch)
        case.main = case.local_cli(case, case.monkeypatch)
    return card(case, capsys)


def ci_takes(case, minutes):
    """The first CI poll of each PR head takes ``minutes`` of clock time."""
    checks, seen = case.tracker.pr_checks, set()

    def poll(pr):
        head = case.tracker.pr_head_sha(pr)
        if (pr, head) not in seen:
            seen.add((pr, head))
            case.clock[0] += timedelta(minutes=minutes)
        return checks(pr)

    case.monkeypatch.setattr(case.tracker, "pr_checks", poll)


def test_runner_review_fail_round_is_wasted_work(runner_case, capsys):
    from test_flow_scorecard_rework import FAIL, PASS, run_once, taking

    case = runner_case
    # 10 min build, 5 min review that fails, 7 min fix build, 3 min passing second review.
    run_once(case, [taking(case, 10, case.implement), taking(case, 7, case.implement)],
             [taking(case, 5, FAIL), taking(case, 3, PASS)])
    entry = issue_parts(runner_card(case, capsys), 1)
    assert {part: entry[part] for part in PARTS} == pytest.approx({
        "value_work": 15, "wasted_work": 10, "necessary_waiting": 0, "wasted_waiting": 2,
    })
    assert entry["efficiency"] == pytest.approx(15 / 27)


def test_runner_records_the_tested_head_and_first_ci_is_value(runner_case, capsys):
    from test_flow_scorecard_rework import PASS, run_once, taking

    case = runner_case
    ci_takes(case, 4)
    run_once(case, [taking(case, 10, case.implement)], [taking(case, 5, PASS)])
    pr = case.tracker.merged_prs[0]
    ci = [row for row in case.store.flow_records()
          if row["kind"] == "activity" and row["issue"] == 1 and row["data"].get("activity") == "ci"]
    assert ci, "The runner records CI as an activity"
    assert {row["data"].get("head") for row in ci} == {case.tracker.pr_head_sha(pr)}, (
        "Each ci activity record names the head CI tested")
    entry = issue_parts(runner_card(case, capsys), 1)
    assert {part: entry[part] for part in PARTS} == pytest.approx({
        "value_work": 19, "wasted_work": 0, "necessary_waiting": 0, "wasted_waiting": 2,
    }), "First build, first review and the first CI are value; the unrecorded 2 minutes are wasted waiting"


# ---------------------------------------------------------------- lead time in five stages
#
# Sources (DECIDE): the issue body's plan-child marker names the order and key; the order's
# preparation record (``<state>/prepare/<order>.json``) dates "ordered" (its first attempt's
# start) and "prepared" (the finish of the reviewer attempt that passed); the issue row's
# ``created_at`` dates "published"; the run start dates "started"; the merge record dates
# "merged". A decision linked by the body's decisions marker dates "decided" from its ledger
# heading; without one the lead time starts at order creation and is marked ``start: ordered``.

STAGES = ("decided->ordered", "ordered->prepared", "prepared->published", "published->started",
          "started->merged")
PLAN = "2026-10-20-lead-time"


def _stamp(moment):
    return moment.isoformat(timespec="seconds")


def lead(case, number, *, ordered, prepared, published, started, work=10, decision=None):
    """An order child: ordered, prepared, published, approved at its start, built ``work`` minutes."""
    from foragent import order_prepare
    from foragent.issue import body_hash
    import json

    key = f"issue-{number}"
    markers = [f"<!-- foragent:plan-child id={PLAN} key={key} -->"]
    if decision is not None:
        markers.append(f'<!-- foragent:decisions ["{decision}"] -->')
    body = ("<!-- foragent:v1 -->\n" + "\n".join(markers) + "\n## Goal\nChange the module.\n## Files\n"
            f"- src/issue_{number}.py\n## Out of scope\nOther modules.\n## Done when\n```sh\ntrue\n```\n")
    path = order_prepare._record_path(case.store, {"id": PLAN})
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"plan": PLAN, "issues": {}}
    written = ordered + (prepared - ordered) / 2
    record["issues"][key] = {
        "key": key, "status": "reviewed", "phase": "reviewing_tests", "questions": [], "notes": [],
        "reason": None, "updated_at": _stamp(prepared), "cycle": 1,
        "attempts": [
            {"attempt": 1, "role": "writer", "cycle": 1, "started_at": _stamp(ordered),
             "finished_at": _stamp(written), "status": "written"},
            {"attempt": 2, "role": "reviewer", "cycle": 1, "started_at": _stamp(written),
             "finished_at": _stamp(prepared), "status": "reviewed"},
        ]}
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    # Published on GitHub; approved later, when its run starts, so approval-to-merge is the build only.
    at(case, started)
    case.store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                             approved_by="human", approved_at=started.isoformat())
    case.store.update_local_issue(number, title=f"Issue {number}", body=body, labels=["approved", "ready"],
                                  state="open", blocked_by=[], created_at=_stamp(published))
    start(case, number, started)
    phase(case, number, "implementing", work, cause="initial")
    merge(case, number)


def lead_time(payload, window="today"):
    metrics = payload["window"][window]
    assert "lead_time" in metrics, f"The scorecard JSON must expose window.{window}.lead_time"
    value = metrics["lead_time"]
    assert isinstance(value, dict) and {"medians", "issues"} <= set(value), (
        "lead_time holds the stage medians and the per-issue stages")
    return value


def lead_entry(payload, number, window="today"):
    entries = [entry for entry in lead_time(payload, window)["issues"] if entry.get("issue") == number]
    assert len(entries) == 1, f"Merged issue #{number} must appear once in lead_time.issues"
    entry = entries[0]
    assert {"start", "stages", "minutes"} <= set(entry), entry
    assert list(entry["stages"]) == list(STAGES), f"The five stages, in order: {entry['stages']}"
    return entry


def test_lead_time_from_order_creation_is_marked_and_split_into_five_stages(flow, capsys):
    ordered = NOW - timedelta(hours=6)
    lead(flow, 81, ordered=ordered, prepared=ordered + timedelta(minutes=40),
         published=ordered + timedelta(minutes=55), started=ordered + timedelta(minutes=95), work=12)
    entry = lead_entry(card(flow, capsys), 81)
    assert entry["start"] == "ordered", "Without a linked decision the lead time starts at order creation, marked"
    assert entry["stages"]["decided->ordered"] is None
    assert {stage: entry["stages"][stage] for stage in STAGES[1:]} == pytest.approx({
        "ordered->prepared": 40, "prepared->published": 15, "published->started": 40, "started->merged": 12,
    })
    assert entry["minutes"] == pytest.approx(107), "Lead time runs from order creation to merge"


def test_lead_time_starts_at_a_linked_decision(flow, capsys):
    from datetime import UTC, datetime

    (flow.root / "docs").mkdir(exist_ok=True)
    (flow.root / "docs" / "decisions.md").write_text(
        "# Decisions\n\n## 2026-10-25\n\n### D7. Measure lead time (2026-10-25, Jin)\n- Decision: yes.\n",
        encoding="utf-8")
    ordered = NOW - timedelta(hours=6)
    lead(flow, 82, ordered=ordered, prepared=ordered + timedelta(minutes=40),
         published=ordered + timedelta(minutes=55), started=ordered + timedelta(minutes=95), work=12,
         decision="D7")
    entry = lead_entry(card(flow, capsys), 82)
    assert entry["start"] == "decided"
    decided = entry["stages"]["decided->ordered"]
    assert type(decided) in (int, float) and decided > 0, entry
    # The ledger dates a decision by day; any reading of that day lies within a day of its midnight.
    midnight = (ordered - datetime(2026, 10, 25, tzinfo=UTC)).total_seconds() / 60
    assert abs(decided - midnight) <= 24 * 60, entry
    assert {stage: entry["stages"][stage] for stage in STAGES[1:]} == pytest.approx({
        "ordered->prepared": 40, "prepared->published": 15, "published->started": 40, "started->merged": 12,
    })
    assert entry["minutes"] == pytest.approx(decided + 107), "Lead time runs from the decision to merge"


def test_scorecard_json_shows_the_daily_median_of_each_stage(flow, capsys):
    def child(number, merged_day, publish_wait):
        ordered = NOW - timedelta(days=merged_day, hours=8) + timedelta(minutes=number)
        published = ordered + timedelta(minutes=30 + publish_wait)
        lead(flow, number, ordered=ordered, prepared=ordered + timedelta(minutes=30), published=published,
             started=published + timedelta(minutes=10), work=10)

    for number, wait in ((91, 10), (92, 20), (93, 90)):  # today: median 20, mean 40
        child(number, 0, wait)
    child(94, 1, 50)
    child(95, 2, 30)
    payload = card(flow, capsys, days=3)
    assert "lead_time_daily" in payload, "The scorecard JSON shows lead_time_daily, one entry per day"
    daily = payload["lead_time_daily"]
    assert len(daily) == 3
    for day in daily:
        assert list(day) == list(STAGES), day
    assert [day["prepared->published"] for day in daily] == pytest.approx([20, 50, 30])
    assert [day["ordered->prepared"] for day in daily] == pytest.approx([30, 30, 30])
    assert [day["published->started"] for day in daily] == pytest.approx([10, 10, 10])
    assert [day["started->merged"] for day in daily] == pytest.approx([10, 10, 10])
    assert [day["decided->ordered"] for day in daily] == [None, None, None]
    assert lead_time(payload)["medians"]["prepared->published"] == pytest.approx(20)
    assert lead_time(payload, "previous")["medians"]["prepared->published"] == pytest.approx(40)
    assert sorted(entry["issue"] for entry in lead_time(payload)["issues"]) == [91, 92, 93]


STAGE_NAME = r"{}\s*(?:->|→|to)\s*{}"


def two_days(case, previous, today):
    """One order child merged per day; stage minutes (ordered->prepared, prepared->published)."""
    for number, merged_day, (preparing, publishing) in ((101, 1, previous), (102, 0, today)):
        ordered = NOW - timedelta(days=merged_day, hours=6)
        prepared = ordered + timedelta(minutes=preparing)
        published = prepared + timedelta(minutes=publishing)
        lead(case, number, ordered=ordered, prepared=prepared, published=published,
             started=published + timedelta(minutes=10), work=10)


def test_report_second_line_names_the_stage_that_worsened_most(flow, capsys):
    two_days(flow, previous=(30, 20), today=(40, 80))  # ordered->prepared +10, prepared->published +60
    payload = card(flow, capsys)
    assert lead_time(payload)["medians"]["prepared->published"] == pytest.approx(80)
    assert lead_time(payload, "previous")["medians"]["prepared->published"] == pytest.approx(20)
    lines = report(flow, capsys)
    assert len(lines) >= 3, "\n".join(lines)
    second = lines[1]
    assert re.search(STAGE_NAME.format("prepared", "published"), second, re.I), (
        f"The /fa report's second line names the stage whose median wait worsened most: {second!r}")
    assert not re.search(STAGE_NAME.format("ordered", "prepared"), second, re.I), second


def test_report_names_no_stage_when_every_stage_improves(flow, capsys):
    two_days(flow, previous=(40, 80), today=(30, 20))
    payload = card(flow, capsys)
    assert lead_time(payload)["medians"]["prepared->published"] == pytest.approx(20)
    assert lead_time(payload, "previous")["medians"]["prepared->published"] == pytest.approx(80)
    lines = report(flow, capsys)
    for stage in STAGES:
        before, after = stage.split("->")
        assert not any(re.search(STAGE_NAME.format(before, after), line, re.I) for line in lines), "\n".join(lines)
