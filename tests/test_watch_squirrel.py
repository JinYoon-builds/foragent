"""Lane rows (#167): acorns from phase events, glyph choice, journal facts and the height budget."""

from datetime import timedelta
import json

import pytest

from foragent import watch
from foragent.config import Config
from foragent.state import StateStore
from foragent.watch_data import journal_facts
from foragent.watch_sections import lanes
from test_watch import NOW


def phase(name, opened, issue=7):
    return {"kind": "phase", "issue": issue, "phase": name, "summary": f"{name} -> next",
            "phase_started_at": opened.isoformat(), "at": opened.isoformat()}


def rows_frame(snapshot, config, root, *, width=120, now=None):
    """The retained lane-rows section (#167) through its own render API.

    The lane scene (lane_blocks) replaces these rows in composed frames; the section and its
    row rules stay callable, so these tests drive it directly.
    """
    lines = lanes.render(snapshot, config, root, tick=0, width=width, selected=None,
                         now=now or snapshot.captured_at, top=0, hits=[], marks=[], rows=True,
                         height=None, state=None)
    return "\n".join("".join(value for value, _ in line).rstrip() for line in lines)


def test_closed_phase_events_of_this_run_decide_the_acorns():
    started = NOW
    events = [phase("implementing", started), phase("checking", started + timedelta(minutes=5))]
    # The run is in review after code and local tests closed.
    assert lanes.finished_acorns("reviewing", events, started.isoformat()) == {0, 1}
    # An earlier run's events do not count for this run.
    old = [phase("implementing", started - timedelta(hours=2)), phase("reviewing", started - timedelta(hours=1))]
    assert lanes.finished_acorns("checking", old, started.isoformat()) == {0}
    # A wait keeps what the run earned; a baseline after checking keeps local tests open.
    assert lanes.finished_acorns("test-slot-wait", events, started.isoformat()) == {0, 1}
    assert lanes.finished_acorns("baseline", events, started.isoformat()) == {0}


@pytest.mark.parametrize("current,expected", [
    ("implementing", set()), ("baseline", set()), ("checking", {0}), ("reviewing", {0, 1}),
    ("ci", {0, 1, 2}), ("merging", {0, 1, 2, 3}), ("resolving", {0, 1, 2}),
    ("overlap-wait", set()), ("needs-human", set()), (None, set()),
])
def test_without_phase_events_the_fixed_order_decides(current, expected):
    assert lanes.finished_acorns(current, [], NOW.isoformat()) == expected


@pytest.mark.parametrize("env,mode", [
    ({"TERM": "xterm", "LANG": "ko_KR.UTF-8"}, "emoji"),
    ({"TERM": "linux", "LANG": "ko_KR.UTF-8"}, "ascii"),
    ({"TERM": "xterm", "LANG": "C"}, "ascii"),
    ({"TERM": "xterm", "LANG": "C", "LC_CTYPE": "en_US.utf8"}, "emoji"),
    ({"TERM": "xterm", "LANG": "en_US.UTF-8", "LC_ALL": "POSIX"}, "ascii"),
    ({"TERM": "xterm"}, "ascii"),
])
def test_auto_glyphs_follow_the_effective_locale(monkeypatch, env, mode):
    for key in ("TERM", "LANG", "LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert lanes.glyph_mode(Config()) == mode
    assert lanes.glyph_mode(Config(view_glyphs="emoji")) == "emoji"


@pytest.mark.parametrize("seconds,ko,en", [(None, "", ""), (59, "0분", "0m"), (3599, "59분", "59m"),
                                           (3600, "1시간 0분", "1h 0m"), (7500, "2시간 5분", "2h 5m")])
def test_elapsed_is_minutes_then_hours(seconds, ko, en):
    assert lanes.elapsed_text(seconds, "ko") == ko and lanes.elapsed_text(seconds, "en") == en


def test_journal_facts_read_every_phase_and_the_latest_say(tmp_path):
    store = StateStore(tmp_path)
    lines = [phase("implementing", NOW), {"kind": "retry", "issue": 7, "summary": "#7: x", "say": "first"},
             *[{"kind": "note", "summary": f"note {n}"} for n in range(8)],
             {"kind": "retry", "issue": 7, "summary": "#7: y", "say": "second"}, phase("checking", NOW, issue=8)]
    (tmp_path / "orch-events.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines) + '{"kind": "phase", "iss', encoding="utf-8")
    phases, say = journal_facts(store)
    assert {number: [event["phase"] for event in found] for number, found in phases.items()} == {
        7: ["implementing"], 8: ["checking"]}
    assert say["say"] == "second"


def busy_snapshot(lanes_count):
    status = {"lanes": {}, "summary": {"working": lanes_count, "blocked": 0, "waiting_human": 0, "queued": 0},
              "active_runs": [], "history": [{"number": 1}]}
    for index in range(lanes_count):
        slug = f"lane{index}"
        status["lanes"][slug] = {"slug": slug, "lane_display": slug, "state": "working", "issue": 100 + index,
                                 "title": f"Task {index}", "phase": "implementing", "elapsed_seconds": 60,
                                 "last_activity_seconds": 0, "queued": [], "running": [], "waiting_for": []}
    events = [{"at": NOW.isoformat(), "kind": "note", "summary": f"handled {n}"} for n in range(3)]
    return watch.Snapshot(status, {}, None, 0, lanes_count, None, events, NOW)


def test_rows_win_and_sections_give_way_from_the_bottom_on_a_short_screen(tmp_path):
    config = Config(language="en", lanes={f"lane{index}": {} for index in range(18)})
    tall = watch._frame(busy_snapshot(18), config, tmp_path, tick=0, width=100, selected=None, now=NOW,
                        height=None)
    # Shell: the heightless frame is unbounded, and a 24-row screen scrolls its body under a
    # pinned top line, above the pinned usage and hint rows; no section is dropped.
    whole = watch.render_frame(busy_snapshot(18), config, tmp_path, width=100).splitlines()
    short = watch.render_frame(busy_snapshot(18), config, tmp_path, width=100, height=24, state={}).splitlines()
    assert len(tall) == len(whole) > 24 == len(short)
    # The lane scene: one block per working lane, each with its acorn, all reachable by scrolling.
    assert sum("◖█#" in line and " write▌" in line for line in whole) == 18
    assert short[0] == whole[0] and short[-2:] == whole[-2:]
    assert short[-2] == "Claude — · Codex — · keep 80%"
    assert short[-1] == "Click an acorn to see what it is · s settings · m usage mode · q quit"
    assert "handled" not in "\n".join(whole), "the r56 bubble summary replaces the events log"


def test_wide_screens_put_the_orchestrator_box_beside_the_sections(tmp_path):
    config = Config(language="en", lanes={"lane0": {}})
    wide = watch.render_frame(busy_snapshot(1), config, tmp_path, width=160).splitlines()
    narrow = watch.render_frame(busy_snapshot(1), config, tmp_path, width=79).splitlines()
    # From 80 columns the big squirrel stands beside its bubble; narrower, the bubble is full width.
    top = next(index for index, line in enumerate(wide) if "╭" in line)
    assert wide[top].index("╭") == 21 and all(any(glyph in line for glyph in "▀▄") for line in wide[top:top + 8])
    edge = next(index for index, line in enumerate(narrow) if line.startswith("╭"))
    assert narrow[edge + 7].startswith("╰") and not any(glyph in line for line in narrow[edge:edge + 8] for glyph in "▀▄")
    assert not any("orchestrator" in line and "┌" in line for line in wide), "no legacy orchestrator box"


def one_lane(state="working", phase="merging", lane="abcdefghijklmno", run=True, history=(), events=()):
    status = {"lanes": {"x": {"slug": "x", "lane_display": lane, "state": state, "issue": 7, "title": "Task",
                              "phase": phase, "elapsed_seconds": 60, "last_activity_seconds": 0,
                              "queued": [{"number": 7, "title": "Task"}] if state == "queued" else [],
                              "running": [], "waiting_for": []}},
              "summary": {"working": 1, "blocked": 0, "waiting_human": 0, "queued": 0},
              "active_runs": [{"number": 7, "lane": "x", "phase": phase, "status": "running", "attempts": 1,
                               "started_at": (NOW - timedelta(minutes=12)).isoformat(),
                               "last_activity_at": NOW.isoformat()}] if run else [],
              "history": list(history)}
    snapshot = watch.Snapshot(status, {}, None, 0, 1, None, [], NOW)
    snapshot.phases = {7: list(events)}
    return snapshot


def row_of(frame):
    return next(line for line in frame.splitlines() if "#7" in line)


@pytest.mark.parametrize("width", [40, 50, 60, 70])
@pytest.mark.parametrize("phase,mode", [("merging", "emoji"), ("merging", "ascii"), ("overlap-wait", "ascii")])
def test_the_five_acorns_stay_at_the_end_however_long_the_other_columns(tmp_path, width, phase, mode):
    """Defect class: trailing columns lost when earlier columns take more cells than the line has."""
    config = Config(language="en", view_glyphs=mode, lanes={"x": {}})
    acorns = {"emoji": ("🌰", "○"), "ascii": ("#", ".")}[mode]
    expected = 4 if phase == "merging" else 0
    row = row_of(rows_frame(one_lane(phase=phase), config, tmp_path, width=width))
    assert row.endswith(acorns[0] * expected + acorns[1] * (5 - expected)), row
    assert watch._cells(row) <= width


def test_a_warning_cell_gives_way_before_the_acorns(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "_activity", lambda *args: "○ " + "warning " * 20)
    config = Config(language="en", view_glyphs="ascii", lanes={"x": {}})
    row = row_of(rows_frame(one_lane(), config, tmp_path, width=60))
    assert row.endswith("####.") and watch._cells(row) <= 60


def test_rows_without_their_own_run_show_no_acorns_of_older_runs(tmp_path):
    """Defect class: acorns counted from phase events without knowing which run they belong to."""
    config = Config(language="en", view_glyphs="ascii", lanes={"x": {}})
    old = NOW - timedelta(hours=3)
    events = [phase("implementing", old), phase("checking", old + timedelta(minutes=5)),
              phase("reviewing", old + timedelta(minutes=9))]
    # A queued issue with no run has earned nothing, whatever an earlier run did.
    queued = one_lane(state="queued", phase=None, run=False, events=events)
    assert row_of(rows_frame(queued, config, tmp_path)).endswith(".....")
    # A blocked issue keeps what its latest recorded run earned, and only that.
    latest = {"number": 7, "started_at": (old + timedelta(minutes=7)).isoformat(), "status": "finished"}
    blocked = one_lane(state="blocked", phase=None, run=False, events=events,
                       history=[{"number": 7, "started_at": (old - timedelta(hours=1)).isoformat()}, latest])
    assert row_of(rows_frame(blocked, config, tmp_path)).endswith("..#..")
    # Events without their own start time cannot be shown to belong to the run.
    undated = [{**event, "phase_started_at": None} for event in events]
    assert lanes.finished_acorns("ci", undated, NOW.isoformat()) == {0, 1, 2}  # The fixed order instead.
    assert lanes.finished_acorns("ci", events, None) == {0, 1, 2}


@pytest.mark.parametrize("width", [1, 2, 3, 5])
def test_very_narrow_frames_finish_rendering(tmp_path, width):
    """Defect class: wrapping given fewer cells than one character after the clamp went away."""
    snapshot = one_lane()
    snapshot.events = [{"at": NOW.isoformat(), "kind": "note", "summary": "검사 완료 handled"}]
    config = Config(language="ko", lanes={"x": {}})
    frame = watch._frame(snapshot, config, tmp_path, tick=0, width=width, selected="x", now=NOW, height=None)
    assert frame and all(watch._cells("".join(value for value, _ in line)) <= width for line in frame)
    assert watch._wrap("한글 word", 1)


def test_every_worker_row_names_its_title_from_the_local_records(tmp_path):
    """Defect class: worker facts taken only from status lists that leave some workers out."""
    from foragent.tracker import FakeTracker
    from test_watch import snapshot as collected

    tracker, store = FakeTracker(), StateStore(tmp_path, now=lambda: NOW)
    config = Config(language="en", view_glyphs="ascii", lanes={"api": {"title": "Server"}})
    for number, title in ((1, "First asks a person"), (2, "Second asks a person")):
        tracker.add_issue(number, title, labels=["approved", "running", "needs-human", "lane:api"])
        store.start_run(number, branch=f"issue-{number}", worktree="unused", lane="api")
    frame = rows_frame(collected(tracker, store, config), config, tmp_path)
    rows = [line for line in frame.splitlines() if line.startswith("[!]")]
    assert len(rows) == 2 and "First asks a person" in rows[0] and "Second asks a person" in rows[1], frame


def test_a_waiting_issue_keeps_its_acorns_after_ten_other_runs(tmp_path):
    """Defect class: the ten-run status history used as if it were the whole history."""
    from foragent.tracker import FakeTracker
    from test_watch import snapshot as collected

    clock = [NOW - timedelta(hours=5)]
    tracker, store = FakeTracker(), StateStore(tmp_path, now=lambda: clock[0])
    config = Config(language="en", view_glyphs="ascii", lanes={"api": {"title": "Server"}})
    tracker.add_issue(5, "Blocked task", labels=["approved", "blocked", "lane:api"])
    store.start_run(5, branch="issue-5", worktree="unused", lane="api")
    started = store.get_run(5).started_at
    store.finish_run(5, "blocked")
    for number in range(20, 31):
        clock[0] += timedelta(minutes=10)
        tracker.add_issue(number, f"Other {number}", labels=["lane:api"], state="closed")
        store.start_run(number, branch=f"issue-{number}", worktree="unused", lane="api")
        store.finish_run(number, "failed")
    opened = datetime_of(started)
    (tmp_path / "orch-events.jsonl").write_text(json.dumps(phase("implementing", opened, issue=5)) + "\n")
    result = collected(tracker, store, config)
    assert 5 not in {run["number"] for run in result.status["history"]}
    row = next(line for line in rows_frame(result, config, tmp_path).splitlines() if "#5" in line)
    assert row.endswith("#...."), row


def datetime_of(value):
    from datetime import datetime

    return datetime.fromisoformat(value)


def test_idle_rows_count_todays_merges_in_the_lane_only(tmp_path):
    """Defect class: the order's done list used where the spec asks for today's count."""
    from foragent.tracker import FakeTracker
    from test_watch import snapshot as collected

    clock = [NOW - timedelta(days=1)]
    tracker, store = FakeTracker(), StateStore(tmp_path, now=lambda: clock[0])
    config = Config(language="en", view_glyphs="ascii", lanes={"docs": {"title": "Docs"}, "api": {"title": "Server"}})
    tracker.add_issue(90, "Order")
    for number, when in ((1, NOW - timedelta(days=1)), (2, NOW - timedelta(minutes=30)), (3, NOW - timedelta(minutes=20))):
        clock[0] = when
        tracker.add_issue(number, f"Docs {number}", labels=["approved", "lane:docs"], state="closed")
        if number != 3:
            tracker.set_parent(number, 90)  # #3 belongs to no open order but merged today.
        store.start_run(number, branch=f"issue-{number}", worktree="unused", lane="docs")
        store.finish_run(number, "merged")
    clock[0] = NOW
    result = collected(tracker, store, config)
    assert result.today_done == {"docs": 2}
    frame = watch.render_frame(result, config, tmp_path).splitlines()
    # The shared idle block lists each lane once; only today's merges in that lane count.
    names = next(line for line in frame if "Docs" in line and "Server" in line)
    entries = dict(entry.split(" ", 1) if " " in entry else (entry, "") for entry in names.split(" | "))
    assert (entries["Docs"], entries["Server"]) == ("v2", ""), frame


def test_a_waiting_row_without_an_active_run_uses_its_latest_recorded_run(tmp_path):
    """Defect class: recorded run facts ignored when a waiting row has no active run."""
    config = Config(language="en", view_glyphs="ascii", lanes={"x": {}})
    latest = {"number": 7, "started_at": (NOW - timedelta(minutes=30)).isoformat(), "phase": "reviewing"}
    for state in ("blocked", "waiting-human"):
        snapshot = one_lane(state=state, phase=None, run=False, history=[latest])
        snapshot.status["lanes"]["x"]["elapsed_seconds"] = None
        row = row_of(rows_frame(snapshot, config, tmp_path))
        # 30 minutes since the run began, and code and local tests reached before it waited.
        assert "30m" in row and row.endswith("##..."), row
    # The full history wins over the ten-run status history.
    snapshot = one_lane(state="blocked", phase=None, run=False, history=[latest])
    snapshot.run_starts, snapshot.run_phases = {7: (NOW - timedelta(minutes=50)).isoformat()}, {7: "ci"}
    row = row_of(rows_frame(snapshot, config, tmp_path))
    assert "50m" in row and row.endswith("###.."), row
    # A queued issue has no run yet: no time and no acorns from an earlier attempt.
    queued = one_lane(state="queued", phase=None, run=False, history=[latest])
    queued.run_starts, queued.run_phases = {7: latest["started_at"]}, {7: "reviewing"}
    row = row_of(rows_frame(queued, config, tmp_path))
    assert "30m" not in row and row.endswith("....."), row


def test_local_tests_need_checking_and_any_baseline_finished_and_stay_earned():
    """Defect class: completion that ignores a combined prerequisite or forgets earned progress."""
    start = NOW
    at = lambda minutes: start + timedelta(minutes=minutes)
    code, checking = phase("implementing", at(1)), phase("checking", at(20))
    baseline = phase("baseline", at(10))
    # Checking closed while the run's first baseline is still being measured or awaited: not yet.
    for current in ("baseline", "baseline-wait"):
        assert lanes.finished_acorns(current, [code, checking], start.isoformat()) == {0}
    # Both closed: earned, and a later baseline (a new base) does not take it back.
    assert lanes.finished_acorns("reviewing", [code, baseline, checking], start.isoformat()) == {0, 1}
    assert lanes.finished_acorns("baseline", [code, baseline, checking], start.isoformat()) == {0, 1}
    # A baseline alone never fills it; a run whose baseline came from the cache had none to finish.
    assert lanes.finished_acorns("checking", [code, baseline], start.isoformat()) == {0}
    assert lanes.finished_acorns("reviewing", [code, checking], start.isoformat()) == {0, 1}
    # Later phases stay earned through waits, a person and a block.
    review = phase("reviewing", at(30))
    for current in ("test-slot-wait", "needs-human", None):
        assert lanes.finished_acorns(current, [code, checking, review], start.isoformat()) == {0, 1, 2}


def test_a_waiting_person_issue_without_a_run_waits_for_a_person_everywhere(tmp_path):
    """Defect class: state markers read only for some of the issues the view shows."""
    from foragent.status import issue_state
    from foragent.tracker import FakeTracker
    from test_watch import snapshot as collected

    tracker, store = FakeTracker(), StateStore(tmp_path, now=lambda: NOW)
    config = Config(language="en", view_glyphs="ascii", lanes={"api": {"title": "Server"}, "web": {"title": "Web"}})
    tracker.add_issue(4, "Parked for a person", labels=["approved", "ready", "waiting-person", "lane:api"])
    tracker.add_issue(5, "Running then parked", labels=["approved", "running", "waiting-person", "lane:web"])
    store.start_run(5, branch="issue-5", worktree="unused", lane="web")
    result = collected(tracker, store, config)
    frame = watch.render_frame(result, config, tmp_path).splitlines()
    rows = rows_frame(result, config, tmp_path).splitlines()
    for number in (4, 5):
        row = next(line for line in rows if f"#{number}" in line)
        assert row.startswith("[!]") and "waiting for a person" in row, rows
        # The lane scene marks it with its recorded person state: a red question acorn.
        assert f"[#{number} question]" in "\n".join(frame), frame
    # The header and gate status count both as waiting, none as queued.
    assert result.status["summary"]["waiting_human"] == 2 and result.status["summary"]["queued"] == 0
    # The top line names no question for parked work (no needs-human issue).
    assert frame[0].startswith("* running fine · today 0 issues merged"), frame[0]
    # One mapping decides every row, in collect_lanes order.
    assert [issue_state(labels, active) for labels, active in (
        ({"waiting-person"}, False), ({"needs-human", "blocked"}, True), ({"blocked"}, True),
        ({"running"}, False), ({"approved"}, False), (set(), True), (set(), False))] == [
        "waiting-human", "waiting-human", "blocked", "working", "queued", "working", None]


@pytest.mark.parametrize("columns,title,elapsed", [(100, True, True), (99, False, True), (60, False, True),
                                                   (59, False, False)])
def test_the_live_terminal_uses_its_own_width_for_the_column_rules(tmp_path, monkeypatch, columns, title, elapsed):
    """Defect class: the live terminal rendering at a different width than the terminal has."""
    import curses
    from types import SimpleNamespace
    from unittest.mock import Mock

    snapshot = one_lane(phase="implementing", lane="api")
    snapshot.status["lanes"]["x"]["title"] = "TITLE_COLUMN"
    config = Config(language="en", view_glyphs="ascii", lanes={"x": {}})
    screen = Mock()
    screen.getmaxyx.return_value = (24, columns)
    screen.getch.return_value = ord("q")
    monkeypatch.setattr(watch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(watch.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(watch, "collect", lambda *args: snapshot)
    monkeypatch.setattr(watch, "time", SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda *args: None)
    monkeypatch.setattr(curses, "wrapper", lambda terminal, *args: terminal(screen, *args))
    watch.run(lambda: {}, Mock(), StateStore(tmp_path), config, tmp_path, now=lambda: NOW)
    painted: dict[int, str] = {}
    for call in screen.addstr.call_args_list:
        y, x, value = call.args[:3]
        painted[y] = painted.get(y, "") + value
    # The lane scene paints at the terminal's own width: the bubble spans it, the acorn carries
    # only its number and step (title and time move into the click explanation).
    row = next(line for line in painted.values() if "#7" in line)
    assert "[#7 write]" in row and "TITLE_COLUMN" not in row and "12m" not in row, row
    border = next(line for line in painted.values() if line.startswith("+-"))
    assert watch._cells(border.rstrip()) == columns, border
    assert all(watch._cells(line) <= columns for line in painted.values())


@pytest.mark.parametrize("meter,queued,expected", [
    ({"running": 1, "budget": 1}, True, "waiting its turn · file overlap: a.py · slots full"),
    ({"running": 0, "budget": 1}, True, "waiting its turn · file overlap: a.py"),
    ({"running": 1, "budget": 1}, False, "waiting its turn · file overlap: a.py"),
    ({"running": 1, "budget": 0}, True, "waiting its turn · file overlap: a.py"),
    ({"running": True, "budget": 1}, True, "waiting its turn · file overlap: a.py"),
])
def test_overlap_wait_also_names_full_slots_from_the_collected_meter(meter, queued, expected):
    from foragent.watch_data import Snapshot
    from foragent.watch_scene import waiting_reason

    snapshot = Snapshot(status={}, done={}, order=None, completed=0, total=0, eta=None, events=[],
                        captured_at=NOW, meter=meter, waiting_facts={2: {"kind": "file-overlap", "blocking_issues": [],
                                           "overlapping_files": ["a.py"]}})
    row = {"issue": 2, "queued": [{"number": 2}] if queued else []}
    assert waiting_reason(snapshot, row, 2, "en") == expected
