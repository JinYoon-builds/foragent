"""A manual Snapshot may leave out any optional fact: the lane scene draws what remains.

Every test walks one complete snapshot, removes one field at a time (or sets it to None)
and draws the whole frame, with no selection and with every acorn selected, so a direct
read anywhere in the scene stops the walk with the name of the removed field.
"""

import copy
from datetime import timedelta

import pytest

from foragent import watch, watch_scene
from foragent.config import Config
from foragent.watch_data import Snapshot
from foragent.watch_sections import lane_blocks, lane_bubble
from test_watch import NOW

ACORNS = range(1, 9)
# Required by the dataclass and read by every section; the scene's own facts stay optional.
OPTIONAL_FIELDS = ("requests", "recent", "heartbeats", "log_ages", "worker_states", "eta", "eta_range",
                   "today", "merged_at", "panel", "titles", "run_phases", "today_done", "meter",
                   "lane_facts", "waiting_facts", "park_facts")
RUN = {"number": 1, "lane": "work", "status": "running", "phase": "implementing", "pid": 12,
       "started_at": (NOW - timedelta(hours=1)).isoformat(),
       "phase_started_at": (NOW - timedelta(minutes=30)).isoformat(),
       "last_activity_at": (NOW - timedelta(minutes=20)).isoformat(),
       "last_progress_at": (NOW - timedelta(minutes=25)).isoformat()}


def _complete() -> dict:
    """Keyword arguments of a Snapshot where every scene branch has its facts."""
    lanes = {
        "work": {"slug": "work", "lane_display": "Work", "state": "working", "issue": 1, "phase": "implementing",
                 "title": "One", "last_activity_seconds": 1200.0, "elapsed_seconds": 3600.0, "waiting_for": [9],
                 "running": [{"number": 1, "title": "One", "state": "working"}, {"number": 2, "title": "Two"}],
                 "held": [{"number": 3, "title": "Held", "state": "blocked", "phase": "overlap-wait"}],
                 "queued": [{"number": 4, "title": "Next", "state": "queued"}], "dreaming": False},
        "wait": {"slug": "wait", "lane_display": "Wait", "state": "blocked", "issue": 5, "phase": "limit-wait",
                 "title": "Five", "waiting_for": [1], "running": [], "held": [],
                 "queued": [{"number": 6, "title": "Parked"}, {"number": 7, "title": "Authored"}]},
        "ask": {"slug": "ask", "lane_display": "Ask", "state": "waiting-human", "issue": 8, "title": "Eight",
                "running": [], "held": [{"number": 8, "title": "Eight", "state": "waiting-human"}], "queued": []},
        "rest": {"slug": "rest", "lane_display": "Rest", "state": "idle", "issue": None, "running": [], "held": [],
                 "queued": [], "dreaming": True},
    }
    return {
        "status": {"lanes": lanes, "active_runs": [dict(RUN), {**RUN, "number": 2}],
                   "issues": {"running": [{"number": 1}, {"number": 2}], "needs-human": [{"number": 8}],
                              "ready": [{"number": 4}]}, "history": []},
        "done": {}, "order": None, "completed": 0, "total": 0, "events": [], "captured_at": NOW,
        "eta": NOW + timedelta(hours=1), "eta_range": (NOW + timedelta(hours=1), NOW + timedelta(hours=2)),
        "recent": 2, "today": 1, "merged_at": {20: NOW - timedelta(minutes=5)},
        "requests": [{"status": "processing", "text": "Request"}, {"status": "done", "text": "Old"}],
        "heartbeats": {"orch": 900.0}, "log_ages": {1: 700.0}, "worker_states": {1: "working", 2: "working"},
        "panel": {"inbox": 2}, "titles": {1: "One"}, "run_phases": {3: "reviewing"}, "today_done": {"work": 1, "rest": 2},
        "meter": {"running": 2, "budget": 2},
        "lane_facts": {"work": {"owned_paths": ["src/"]}},
        "waiting_facts": {4: {"kind": "file-overlap", "overlapping_files": ["a.py"], "blocking_issues": []},
                          5: {"kind": "machine", "blocking_issues": [1]},
                          7: {"kind": None, "lane": "wait", "title": "Authored",
                              "authored": {"text": "Waiting for the review.", "reason": "Review",
                                           "next_condition": "Approved", "responsible_role": "Reviewer"}}},
        "park_facts": {6: {"parked": True}},
    }


def _paths(value, path=()):
    """Every removable field below ``value``, as a key path."""
    children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, child in children:
        yield (*path, key)
        yield from _paths(child, (*path, key))


FACTS = ("status", "merged_at", "requests", "heartbeats", "log_ages", "worker_states", "panel", "titles",
         "run_phases", "today_done", "meter", "lane_facts", "waiting_facts", "park_facts")
REMOVED = sorted({path for name in FACTS for path in _paths(_complete()[name], (name,))}, key=repr)
MODES = [pytest.param(120, "emoji", id="pixels"), pytest.param(30, "emoji", id="narrow"),
         pytest.param(120, "ascii", id="ascii")]


def _without(path, how):
    facts = _complete()
    parent = facts
    for key in path[:-1]:
        parent = parent[key]
    if how == "none":
        parent[path[-1]] = None
    else:
        del parent[path[-1]]
    return Snapshot(**facts)


def _config(glyphs):
    return Config(language="en", view_glyphs=glyphs, lanes={"work": {"paths": ["src/"]}})


def _frames(snapshot, config, tmp_path, width):
    """The whole frame with nothing selected, then with each acorn selected."""
    for target in (None, *(("acorn", number) for number in ACORNS)):
        state = {} if target is None else {"explanation_target": target}
        yield watch.render_frame(copy.copy(snapshot), config, tmp_path, width=width, now=NOW, state=state,
                                 layout={})


@pytest.fixture(autouse=True)
def _pixel_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")


def test_the_complete_snapshot_reaches_every_acorn_class_the_walk_depends_on():
    """The walk proves nothing unless the snapshot it damages reaches each scene branch."""
    snapshot = Snapshot(**_complete())
    classes = {}
    for row in snapshot.status["lanes"].values():
        current, upcoming = watch_scene.lane_items(snapshot, row)
        classes.update({item["number"]: item["class"] for item in current + upcoming})
    assert classes == {1: "working", 2: "working", 3: "wait", 4: "next", 5: "wait", 6: "parked",
                       7: "authored", 8: "question"}
    assert len(REMOVED) > 100


@pytest.mark.parametrize("width, glyphs", MODES)
@pytest.mark.parametrize("how", ["deleted", "none"])
def test_no_missing_status_or_fact_field_stops_the_whole_view(width, glyphs, how, tmp_path):
    config = _config(glyphs)
    for path in REMOVED:
        try:
            for frame in _frames(_without(path, how), config, tmp_path, width):
                assert isinstance(frame, str)
        except Exception as error:  # Name the field: one test covers every path of the walk.
            pytest.fail(f"{how} {path!r}: {error!r}")


@pytest.mark.parametrize("name", OPTIONAL_FIELDS)
@pytest.mark.parametrize("glyphs", ["emoji", "ascii"])
def test_no_scene_section_needs_a_snapshot_fact_that_is_none(name, glyphs, tmp_path):
    facts = _complete()
    facts[name] = None
    snapshot, config = Snapshot(**facts), _config(glyphs)
    for target in (None, *(("acorn", number) for number in ACORNS)):
        state = {} if target is None else {"explanation_target": target}
        assert lane_bubble.render(snapshot, config, tmp_path, width=120, now=NOW, state=state)
        assert lane_blocks.render(snapshot, config, tmp_path, width=120, now=NOW)
    assert isinstance(watch.render_frame(snapshot, config, tmp_path, width=120, now=NOW, state={}, layout={}), str)


@pytest.mark.parametrize("value", ["deleted", None, {"work": None}])
def test_the_header_reads_the_lanes_when_no_question_answers_first(value, tmp_path):
    """The whole-frame walk keeps a question, and a question skips the header's lane read."""
    facts = _complete()
    facts["status"]["issues"] = {}
    if value == "deleted":
        del facts["status"]["lanes"]
    else:
        facts["status"]["lanes"] = value
    frame = watch.render_frame(Snapshot(**facts), _config("emoji"), tmp_path, width=120, now=NOW, state={}, layout={})
    assert watch.text("lane_header_resting", "en") in frame


def test_a_slugless_lane_row_keeps_its_name_acorns_and_runs_from_the_status_key(tmp_path):
    """The status keys lanes by slug, so a row without its own slug is still that lane."""
    facts = _complete()
    del facts["status"]["lanes"]["work"]["slug"]
    del facts["status"]["lanes"]["work"]["lane_display"]
    snapshot, layout = Snapshot(**facts), {}
    frame = watch.render_frame(snapshot, _config("emoji"), tmp_path, width=120, now=NOW, state={}, layout=layout)
    hits = {hit[4] for hit in layout["hits"]}
    assert {("acorn", 1), ("acorn", 2), ("acorn", 3), ("acorn", 4), ("squirrel", "work")} <= hits
    assert "work" in frame and "src/" in frame
    explained = lane_bubble.explain(("acorn", 1), snapshot, _config("emoji"), tmp_path, now=NOW, width=80, state={})
    assert "work" in "".join(value for row in explained for value, _ in row)


def test_a_lane_row_called_directly_without_a_slug_only_loses_its_lane_bound_facts():
    """No status key to fall back on: a lane-less run stays with no lane, never with this row."""
    row = {"running": [{"number": 1}, {"title": "no number"}, 7], "queued": [{"title": "no number"}],
           "held": [{"state": "blocked"}]}
    run = {k: v for k, v in RUN.items() if k != "lane"}
    snapshot = Snapshot(status={"lanes": {}, "active_runs": [run, {"lane": "work"}]}, done={}, order=None, completed=0,
                        total=0, eta=None, events=[], captured_at=NOW)
    current, upcoming = watch_scene.lane_items(snapshot, row)
    assert [item["number"] for item in current] == [1] and upcoming == []
    assert current[0]["run"] is None


def test_a_numberless_item_leaves_the_others_and_the_counts_alone(tmp_path):
    facts = _complete()
    work = facts["status"]["lanes"]["work"]
    del work["running"][1]["number"]
    del work["queued"][0]["number"]
    snapshot, layout = Snapshot(**facts), {}
    frame = watch.render_frame(snapshot, _config("emoji"), tmp_path, width=120, now=NOW, state={}, layout=layout)
    hits = {hit[4] for hit in layout["hits"]}
    # Issue 2 still has its active run; the queued item had nothing to point at.
    assert {("acorn", 1), ("acorn", 2), ("acorn", 3)} <= hits and ("acorn", 4) not in hits
    # N is the running lists' length (v2.scene.02), whatever an item holds.
    assert "2 issues" in frame


def test_missing_lanes_or_runs_draw_the_empty_scene(tmp_path):
    facts = _complete()
    del facts["status"]["lanes"]
    del facts["status"]["active_runs"]
    snapshot = Snapshot(**facts)
    assert watch_scene.lane_rows(snapshot.status) == []
    assert lane_blocks.render(snapshot, _config("emoji"), tmp_path, width=120, now=NOW)  # The evidence row.
    assert "Nothing to do now" in "\n".join("".join(v for v, _ in line) for line in lane_bubble.render(
        snapshot, _config("emoji"), tmp_path, width=120, now=NOW, state={}))


@pytest.mark.parametrize("key", ["q", "f", "a", "j"])
def test_the_live_view_keeps_taking_keys_without_a_lanes_field(key, tmp_path, monkeypatch):
    """The key loop reads the lanes as well as the frame; both must survive their absence."""
    import curses
    from types import SimpleNamespace
    from unittest.mock import Mock

    from foragent import watch_terminal
    from foragent.state import StateStore

    facts = _complete()
    del facts["status"]["lanes"]
    snapshot = Snapshot(**facts)
    screen = Mock()
    screen.getmaxyx.return_value = (40, 100)
    screen.getch.side_effect = [ord(key), ord("q")]
    monkeypatch.setattr(watch, "time", SimpleNamespace(monotonic=lambda: 0))
    monkeypatch.setattr(watch, "_installed_build", lambda: "unknown")
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda *args: None)
    monkeypatch.setattr(curses, "mousemask", lambda *args: None, raising=False)
    monkeypatch.setattr(curses, "mouseinterval", lambda *args: None, raising=False)
    watch_terminal._terminal(screen, lambda: snapshot, snapshot, _config("emoji"), tmp_path,
                             lambda: snapshot.captured_at, StateStore(tmp_path))
    assert screen.refresh.called


def test_only_a_replaced_section_may_fail_without_stopping_the_view(tmp_path, monkeypatch):
    """The guard is for rows that are dropped anyway; a drawn section's error still surfaces."""
    from types import SimpleNamespace

    from foragent import watch_layout

    def broken(*args, **context):
        raise KeyError("summary")

    def new(*args, **context):
        return [[("new", "")]]

    old = SimpleNamespace(__name__="foragent.watch_sections.test_old", ORDER=1, render=broken)
    replacing = SimpleNamespace(__name__="foragent.watch_sections.test_new", ORDER=2, render=new,
                                REPLACES=(old.__name__,))
    snapshot = Snapshot(**_complete())
    frame = dict(tick=0, width=80, selected=None, now=NOW, state={}, layout={})
    monkeypatch.setattr(watch_layout, "_sections", lambda: [old, replacing])
    assert "new" in "".join(value for line in watch_layout._frame(snapshot, _config("emoji"), tmp_path, **frame)
                            for value, _ in line)
    monkeypatch.setattr(watch_layout, "_sections", lambda: [old])
    with pytest.raises(KeyError):
        watch_layout._frame(snapshot, _config("emoji"), tmp_path, **frame)


def test_an_authored_wait_without_any_text_keeps_its_heading():
    from foragent.watch_waiting import explanation

    rows = explanation({"reason": None}, "en", 60)
    assert ["".join(value for value, _ in row) for row in rows] == ["  " + watch.text("waiting_view_heading", "en")]
