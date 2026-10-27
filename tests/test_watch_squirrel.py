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
    short = watch.render_frame(busy_snapshot(18), config, tmp_path, width=100).splitlines()
    assert len(tall) > 24 >= len(short)
    assert sum(line.startswith("🐿") or line.startswith("[*]") for line in short) == 18
    assert short[-1] == "q: quit · s · m · j/k"
    assert "handled" not in "\n".join(short), "the events log below the rows gives way first"


def test_wide_screens_put_the_orchestrator_box_beside_the_sections(tmp_path):
    config = Config(language="en", lanes={"lane0": {}})
    wide = watch.render_frame(busy_snapshot(1), config, tmp_path, width=160).splitlines()
    narrow = watch.render_frame(busy_snapshot(1), config, tmp_path, width=120).splitlines()
    assert any("┌" in line and "orchestrator" in line and not line.startswith("┌") for line in wide)
    assert any(line == "🧭 orchestrator" for line in narrow) and not any("┌🧭" in line for line in narrow)
