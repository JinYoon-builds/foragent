"""Acceptance tests for shelf-object: one object for every place where Foragent work accumulates.

Only public entry points are driven: the object's own constructor (``foragent.shelf.Place``) and
place list, `fa gate tick` (the real CLI, offline FakeTracker; `scheduler.tick` in the drafting
flow, as the locked drafting tests drive it), `fa gate run-draft`, the runner (`run_issue`),
`fa orch retry`, `fa orch inbox --json`, `fa brief --json` and `fa brief --report`. No process is
started or signalled. The closed-issue slot case is in test_shelf_object_held_slot.py.

The contract read here (place names, brief keys, alarm records) is written down in
shelf_object_support.py.
"""

from datetime import timedelta
import json
import os
from pathlib import Path
import re

import pytest

from shelf_object_support import (
    HELD_AGE, LOGS_AGE, PLACES, WriteWatch, alarms, brief, cleared, cli, hold, issue_refs, part,
    quiet_kicks, report_lines, shelf_module, shelf_row, tick_at,
)
from test_lane_test_drafts import draft_sessions, drafts, placeholder, refresh, run_session, ticks  # noqa: F401
from test_log_retention import log_retention_finished_log
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import tick_case  # noqa: F401 (fixture)


NINE = ("purpose", "reader", "why", "trigger", "channel", "alarm", "capacity", "removal", "person_load")
SUBFIELDS = {"alarm": ("count", "age"), "capacity": ("in", "out"), "person_load": ("cap", "form")}
JUDGMENT = "Judgment: wait for the schema decision before ordering more work."


def complete_fields():
    return {
        "purpose": "Keep acceptance drafts until the orchestrator publishes them",
        "reader": "orchestrator",
        "why": "Drafts that nobody opens pile up in the state folder",
        "trigger": "a drafting session finished",
        "channel": "orchestrator request box",
        "alarm": {"count": 20, "age": timedelta(days=7)},
        "capacity": {"in": "draft folders written", "out": "draft folders removed"},
        "removal": "remove a draft when its issue is closed",
        "person_load": {"cap": 0, "form": "multiple-choice"},
    }


def place_mentions(text, name):
    """Mentions of a place name as a word, not as part of a path (logs/issue-7.log) or of
    another word (run-records, logs-old)."""
    return re.findall(rf"(?<![\w/.-]){re.escape(name)}(?![\w/-])", text)


def minutes(delta):
    return int(delta.total_seconds() // 60)


# ---------------------------------------------------------------- statement 1: nine fields


def test_a_place_is_created_only_with_all_nine_fields():
    shelf = shelf_module()
    place = shelf.Place(name="acceptance-sample", **complete_fields())
    for field, value in complete_fields().items():
        assert getattr(place, field) == value, f"The created place keeps its {field}"

    for field in NINE:
        fields = complete_fields()
        del fields[field]
        with pytest.raises((TypeError, ValueError)) as refused:
            shelf.Place(name="acceptance-sample", **fields)
        assert field in str(refused.value), f"The refusal names the missing field {field}: {refused.value}"
        for empty in (None, ""):
            fields = {**complete_fields(), field: empty}
            with pytest.raises((TypeError, ValueError)) as refused:
                shelf.Place(name="acceptance-sample", **fields)
            assert field in str(refused.value), f"An empty {field} is refused by name: {refused.value}"

    for field, keys in SUBFIELDS.items():
        for key in keys:
            fields = complete_fields()
            fields[field] = {name: value for name, value in fields[field].items() if name != key}
            with pytest.raises((TypeError, ValueError)) as refused:
                shelf.Place(name="acceptance-sample", **fields)
            message = str(refused.value)
            assert field in message or key in message, f"{field} without {key} is refused by name: {message}"


def test_every_phase_one_place_has_its_nine_fields_and_brief_shows_each_place(tick_case, capsys):
    shelf = shelf_module()
    places = {place.name: place for place in shelf.places()}
    assert set(PLACES) <= set(places), f"Phase 1 puts runs, logs, held runs and drafts on the object: {sorted(places)}"
    for name, place in places.items():
        fields = {field: getattr(place, field, None) for field in NINE}
        for field, value in fields.items():
            assert value not in (None, "", {}, []), f"Place {name} has no {field}"
        count, age = part(place.alarm, "count"), part(place.alarm, "age")
        assert isinstance(count, int) and count > 0, f"Place {name} needs a count limit: {place.alarm}"
        assert isinstance(age, timedelta) and age > timedelta(0), f"Place {name} needs an age limit: {place.alarm}"
        for field, keys in SUBFIELDS.items():
            for key in keys:
                assert part(fields[field], key) not in (None, ""), f"Place {name} has no {field} {key}"
        shelf.Place(name=name, **fields)  # A registered place passes the same check as a new one.
    assert part(places["held-runs"].alarm, "age") == HELD_AGE, "Held runs alarm after 15 minutes"

    case = tick_case
    tick_at(case, capsys, case.clock[0] + timedelta(minutes=5))
    shown = brief(case, capsys)
    for name in places:
        row = shelf_row(shown, name)
        for key in ("count", "in_24h", "out_24h"):
            assert isinstance(row[key], int) and not isinstance(row[key], bool) and row[key] >= 0, (name, row)
        assert row["oldest_minutes"] is None or isinstance(row["oldest_minutes"], int), (name, row)


# ---------------------------------------------------------------- statement 2: one alarm per episode


def test_count_limit_alarms_the_owner_once_per_episode_and_records_one_clear(tick_case, capsys):
    case = tick_case
    start = case.clock[0]
    for number in (41, 42, 43):
        hold(case, number)
    tick_at(case, capsys, start + timedelta(minutes=5))
    assert alarms(case, capsys, "held-runs") == [], "Three held runs are at the count limit of 3, not past it"
    assert shelf_row(brief(case, capsys), "held-runs")["count"] == 3

    hold(case, 44)
    tick_at(case, capsys, start + timedelta(minutes=10))
    rows = alarms(case, capsys, "held-runs")
    assert len(rows) == 1, f"A fourth held run passes the count limit and raises one alarm: {rows}"
    first = rows[0]
    assert first["to"] == "pm", "The owner of held runs' reader (the orchestrator) is the PM"
    assert first.get("status") != "done"
    assert shelf_row(brief(case, capsys), "held-runs")["count"] == 4

    for at in (15, 20):  # Still over (and now past the age limit too): the same episode.
        tick_at(case, capsys, start + timedelta(minutes=at))
        assert [row["id"] for row in alarms(case, capsys, "held-runs")] == [first["id"]], (
            "While the place stays over its limit no second alarm is raised")
    assert cleared(case, "held-runs") == [], "Nothing is cleared while the place is over"

    for number in (41, 42, 43, 44):
        case.tracker.close_issue(number)
    for at in (25, 30):
        tick_at(case, capsys, start + timedelta(minutes=at))
        assert len(cleared(case, "held-runs")) == 1, "When the place clears, one cleared fact is recorded"
    assert shelf_row(brief(case, capsys), "held-runs")["count"] == 0
    assert len(alarms(case, capsys, "held-runs")) == 1

    for number in (45, 46, 47, 48):  # A new episode after the clear.
        hold(case, number)
    tick_at(case, capsys, start + timedelta(minutes=35))
    rows = alarms(case, capsys, "held-runs")
    assert len(rows) == 2 and first["id"] in {row["id"] for row in rows}, (
        f"A second episode raises exactly one new alarm: {rows}")
    for number in (45, 46, 47, 48):
        case.tracker.close_issue(number)
    for at in (40, 45):
        tick_at(case, capsys, start + timedelta(minutes=at))
    assert len(cleared(case, "held-runs")) == 2, "Each episode's end is recorded once"
    assert len(alarms(case, capsys, "held-runs")) == 2


# ---------------------------------------------------------------- statement 3: owner, then PM, then report


def test_unread_alarm_moves_from_owner_to_pm_and_then_to_the_report_second_line(tick_case, capsys):
    from foragent.orch_events import append_event

    case = tick_case
    # A log no finished run owns is never removed by age, so the logs place stays over its age limit.
    orphan = case.store.root / "logs" / "issue-77.log"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("left behind\n", encoding="utf-8")
    written = (case.clock[0] - LOGS_AGE - timedelta(days=1)).timestamp()
    os.utime(orphan, (written, written))

    def judge():
        append_event(case.store, case.clock[0], None, "note", "Agent's current judgment", say=JUDGMENT)

    def line_two():
        judge()
        lines = report_lines(case, capsys)
        assert len(lines) >= 2, lines
        return lines[1]

    def pm_alarm_ids():
        listed = brief(case, capsys).get("pm_alarms")
        assert isinstance(listed, list), "fa brief --json lists the open PM alarms"
        return {row.get("id") for row in listed}

    first = case.clock[0] + timedelta(minutes=5)
    tick_at(case, capsys, first)
    owner = alarms(case, capsys, "logs", to="orchestrator")
    assert len(owner) == 1, f"The logs place over its age limit alarms its reader's owner once: {owner}"
    assert alarms(case, capsys, "logs", to="pm") == [], "The PM is not told before the reader had its time"
    assert owner[0]["id"] not in pm_alarm_ids()
    assert line_two().startswith(JUDGMENT[:40]), "The report does not carry an alarm the owner still has"

    tick_at(case, capsys, first + LOGS_AGE - timedelta(minutes=5))
    assert alarms(case, capsys, "logs", to="pm") == [], "No PM alarm before the age limit after the alarm"

    escalated = first + LOGS_AGE + timedelta(minutes=5)
    tick_at(case, capsys, escalated)
    pm = alarms(case, capsys, "logs", to="pm")
    assert len(pm) == 1, f"The reader did not take items out within the limit: the PM gets one alarm: {pm}"
    assert pm[0]["id"] in pm_alarm_ids(), "fa brief --json lists the open PM alarm"
    assert line_two().startswith(JUDGMENT[:40]), "The report waits one more limit after the PM alarm"

    tick_at(case, capsys, escalated + LOGS_AGE - timedelta(minutes=5))
    assert line_two().startswith(JUDGMENT[:40])
    tick_at(case, capsys, escalated + LOGS_AGE + timedelta(minutes=5))
    line = line_two()
    assert not line.startswith(JUDGMENT[:20]), (
        f"After the PM's limit the shelf alarm, not the judgment, opens the /fa report's second line: {line!r}")
    head = line.split(JUDGMENT[:20])[0]
    assert len(place_mentions(head, "logs")) == 1, (
        f"Line 2 opens with exactly one report-level alarm, for the logs place: {line!r}")
    for other in PLACES:
        if other != "logs":
            assert not place_mentions(head, other), f"No alarm for {other}, which is not over: {line!r}"
    tick_at(case, capsys, case.clock[0] + timedelta(minutes=5))
    assert line_two() == line, "Another tick while still over leaves the report line unchanged (not doubled)"
    assert len(alarms(case, capsys, "logs", to="orchestrator")) == 1, "Each level is told once"
    assert len(alarms(case, capsys, "logs", to="pm")) == 1, "Each level is told once"

    orphan.unlink()  # The reader takes the item out.
    tick_at(case, capsys, case.clock[0] + timedelta(minutes=5))
    assert len(cleared(case, "logs")) == 1, "The cleared episode is recorded once"
    assert pm[0]["id"] not in pm_alarm_ids(), "A cleared alarm is no longer an open PM alarm"
    assert line_two().startswith(JUDGMENT[:40]), "A cleared alarm leaves the report"
    tick_at(case, capsys, case.clock[0] + timedelta(minutes=5))
    assert len(cleared(case, "logs")) == 1


# ---------------------------------------------------------------- statement 4: removal by the rule


def run_record_age(path, now):
    value = json.loads(path.read_text(encoding="utf-8"))
    from datetime import datetime

    at = value.get("finished_at") if value.get("status") == "finished" and value.get("finished_at") else value["started_at"]
    return now - datetime.fromisoformat(at)


def test_finished_run_records_and_logs_are_removed_by_their_rule_and_no_longer_counted(tick_case, capsys):
    from datetime import UTC, datetime

    case = tick_case
    store = case.store
    old = log_retention_finished_log(store, case.clock, 61, days=8, outcome="merged")
    young = log_retention_finished_log(store, case.clock, 62, days=2, outcome="merged")
    old_record, young_record = store.root / "runs" / "61.json", store.root / "runs" / "62.json"
    assert old_record.is_file() and young_record.is_file()

    tick_at(case, capsys, case.clock[0] + timedelta(minutes=5))
    now = case.clock[0]
    assert not old.exists() and young.is_file(), "A merged run's log goes after 7 days, a younger one stays"
    assert not old_record.exists(), "The finished run record goes by the same rule as its logs"
    assert young_record.is_file(), "A younger finished run record stays"
    shown = brief(case, capsys)

    log_files = [path for path in (store.root / "logs").rglob("*") if path.is_file()]
    logs = shelf_row(shown, "logs")
    assert logs["count"] == len(log_files), f"Removed logs are no longer counted: {logs} vs {log_files}"
    assert logs["out_24h"] == 1, f"One log left the place in the last 24 hours: {logs}"
    oldest = min(datetime.fromtimestamp(path.stat().st_mtime, UTC) for path in log_files)
    assert logs["oldest_minutes"] == minutes(now - oldest) == minutes(timedelta(days=2, minutes=5)), logs

    records = sorted((store.root / "runs").glob("*.json"))
    runs = shelf_row(shown, "run-records")
    assert runs["count"] == len(records), f"Removed run records are no longer counted: {runs} vs {records}"
    assert runs["out_24h"] == 1, f"One run record left the place in the last 24 hours: {runs}"
    assert runs["oldest_minutes"] == max(minutes(run_record_age(path, now)) for path in records), runs

    tick_at(case, capsys, now + timedelta(hours=25))
    later = brief(case, capsys)
    assert shelf_row(later, "logs")["count"] == logs["count"], "Nothing else was removed"
    assert shelf_row(later, "run-records")["count"] == runs["count"]
    for name in ("logs", "run-records"):
        row = shelf_row(later, name)
        assert row["out_24h"] == 0 and row["in_24h"] == 0, f"In and out count the last 24 hours only: {row}"


def test_a_closed_issues_draft_is_removed_by_the_drafts_rule_and_no_longer_counted(drafts, capsys):  # noqa: F811
    from foragent.orch_draft import draft_dir

    case = drafts()
    placeholder(case, 11, "orch")
    placeholder(case, 21, "view")
    ticks(case, 2)
    sessions = draft_sessions(case)
    assert len(sessions) == 2, case.launched
    for argv in sessions:
        run_session(case, argv)
    assert draft_dir(case.store, 11).is_dir() and draft_dir(case.store, 21).is_dir()
    refresh(case)
    ticks(case)
    row = shelf_row(brief(case, capsys), "drafts")
    assert (row["count"], row["in_24h"], row["out_24h"]) == (2, 2, 0), f"Two drafts were written today: {row}"
    assert isinstance(row["oldest_minutes"], int) and row["oldest_minutes"] >= 0, row

    case.tracker.close_issue(11)
    refresh(case)
    ticks(case)
    assert not draft_dir(case.store, 11).exists(), "A closed issue's draft is finished and removed"
    assert draft_dir(case.store, 21).is_dir(), "An open placeholder's draft stays"
    for _ in range(2):
        row = shelf_row(brief(case, capsys), "drafts")
        assert (row["count"], row["in_24h"], row["out_24h"]) == (1, 2, 1), (
            f"The removed draft is no longer counted and left once: {row}")
        refresh(case)
        ticks(case)


# ---------------------------------------------------------------- statement 6: retries-exceeded without action


def test_retries_exceeded_without_reader_action_alarms_the_pm_after_fifteen_minutes(tick_case, capsys, monkeypatch):
    case = tick_case
    quiet_kicks(monkeypatch)
    start = case.clock[0]
    hold(case, 731)
    hold(case, 742)
    for at in (5, 10):
        tick_at(case, capsys, start + timedelta(minutes=at))
        assert alarms(case, capsys, "held-runs") == [], "No alarm while the held runs are younger than 15 minutes"
    case.clock[0] = start + timedelta(minutes=11)
    cli(case, capsys, "orch", "retry", "742")  # The reader acts on #742 only.
    tick_at(case, capsys, start + timedelta(minutes=14))
    assert alarms(case, capsys, "held-runs") == [], "No alarm one minute before the 15-minute limit"

    tick_at(case, capsys, start + timedelta(minutes=16))
    rows = alarms(case, capsys, "held-runs")
    assert len(rows) == 1, f"#731 held 16 minutes with no reader action raises one alarm: {rows}"
    alarm = rows[0]
    assert alarm["to"] == "pm" and alarm.get("status") != "done", alarm
    named = issue_refs(alarm)
    assert 731 in named and 742 not in named, f"The alarm names the unanswered hold only: {alarm}"
    listed = brief(case, capsys).get("pm_alarms")
    assert isinstance(listed, list) and alarm["id"] in {row.get("id") for row in listed}, (
        f"fa brief --json lists the open PM alarm: {listed}")

    for at in (21, 26):
        tick_at(case, capsys, start + timedelta(minutes=at))
    assert [row["id"] for row in alarms(case, capsys, "held-runs")] == [alarm["id"]], "One alarm per episode"
    row = shelf_row(brief(case, capsys), "held-runs")
    assert (row["count"], row["oldest_minutes"]) == (1, 26), f"Only #731 is held, for 26 minutes: {row}"


# ---------------------------------------------------------------- statement 7: writes go through the object


def test_the_write_watch_sees_bypass_writes_by_destination_not_by_spelling(tmp_path):
    """A check of the guard itself: variable and absolute paths are caught, reads and other files are not."""
    state = tmp_path / "state"
    (state / "runs").mkdir(parents=True)
    (state / "runs" / "5.json").write_text("{}", encoding="utf-8")
    with WriteWatch(state) as watch:
        destination = os.path.join(os.path.realpath(state), "runs", "9.json")
        with open(destination, "w", encoding="utf-8") as stream:
            stream.write("{}")
        Path(str(state) + "/logs").mkdir()  # Creating the folder is not writing a log.
        Path(str(state) + "/logs/issue-9.log").write_text("bypass\n", encoding="utf-8")
        (state / "runs" / "issue-9.worktree.lock").touch()  # Locks are internal layout, not run records.
        os.replace(destination, state / "drafts-moved.json")
        (state / "runs" / "5.json").read_text(encoding="utf-8")
        (state / "elsewhere.json").write_text("{}", encoding="utf-8")
        exec(compile("open(target, 'w').close()", "<object>", "exec"),
             {"__name__": "foragent.shelf", "target": str(state / "runs" / "10.json")})
    assert len(watch.violations) == 3, watch.violations
    assert watch.areas() == {"runs", "logs"}, watch.writes
    assert sum(path.endswith("10.json") for _area, _event, path in watch.writes) == 1


def test_the_write_watch_follows_symlink_aliases_to_their_destination(tmp_path):
    """A check of the guard itself: a write through an alias that points into logs/, drafts/ or
    runs/ is classified by where it lands, whether the alias sits inside or outside the state folder."""
    state, outside = tmp_path / "state", tmp_path / "outside"
    for name in ("logs", "drafts", "runs"):
        (state / name).mkdir(parents=True)
    outside.mkdir()
    os.symlink(state / "logs", state / "log-alias")
    os.symlink(state / "drafts", outside / "drafts-alias")
    os.symlink(state / "runs" / "12.json", state / "record-alias.json")
    (state / "runs" / "12.json").write_text("{}", encoding="utf-8")
    with WriteWatch(state) as watch:
        (state / "log-alias" / "issue-9.log").write_text("bypass\n", encoding="utf-8")
        (outside / "drafts-alias" / "9").mkdir()
        (outside / "drafts-alias" / "9" / "draft.md").write_text("bypass\n", encoding="utf-8")
        (state / "record-alias.json").write_text("{}", encoding="utf-8")
        (state / "log-alias" / "issue-9.log").read_text(encoding="utf-8")
    assert sorted(area for area, _event, _path in watch.writes) == ["drafts", "drafts", "logs", "runs"], watch.writes
    assert len(watch.violations) == 4, watch.violations
    real_logs = os.path.realpath(state / "logs")
    assert any(path == os.path.join(real_logs, "issue-9.log") for _a, _e, path in watch.writes), watch.writes


def test_the_runner_writes_run_records_and_logs_only_through_the_object(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch)
    with WriteWatch(case.store.root) as watch:
        assert run_issue(1, case.ctx) == "merged"
    assert {"runs", "logs"} <= watch.areas(), f"The run wrote its record and log: {watch.writes}"
    assert watch.violations == [], "Run records and logs written outside the object:\n" + "\n".join(watch.violations)


def test_a_drafting_session_writes_drafts_records_and_logs_only_through_the_object(drafts):  # noqa: F811
    case = drafts()
    placeholder(case, 11, "orch")
    with WriteWatch(case.store.root) as watch:
        ticks(case, 2)
        sessions = draft_sessions(case)
        assert len(sessions) == 1, case.launched
        run_session(case, sessions[0])
    assert {"runs", "logs", "drafts"} <= watch.areas(), f"The session wrote its draft, record and log: {watch.writes}"
    assert watch.violations == [], "Drafts, records or logs written outside the object:\n" + "\n".join(watch.violations)


def test_the_gate_removes_expired_logs_and_records_only_through_the_object(tick_case, capsys):
    case = tick_case
    old = log_retention_finished_log(case.store, case.clock, 61, days=31, outcome="failed")
    with WriteWatch(case.store.root) as watch:
        tick_at(case, capsys, case.clock[0] + timedelta(minutes=5))
    assert not old.exists(), "The tick removes a 31-day-old failed run's log"
    assert any(area == "logs" and event == "os.remove" for area, event, _path in watch.writes), watch.writes
    assert watch.violations == [], "Logs or run records removed outside the object:\n" + "\n".join(watch.violations)
