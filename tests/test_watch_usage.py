"""The usage line and the m key of the lane view (#115)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json

import pytest

from foragent.config import Config
from foragent.watch import Snapshot, handle_key, render_frame
from foragent.watch_data import meter as read_meter
from foragent.watch_sections import help as help_section, usage

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def config(**fields):
    return Config(**{"language": "en", "lanes": {}, "skill_libraries": [], **fields})


@pytest.fixture
def store(tmp_path):
    from foragent.state import StateStore

    root = tmp_path / "machine" / "repo"
    root.mkdir(parents=True)
    return StateStore(root)


def snapshot(meter=None, active_runs=()):
    status = {"lanes": {}, "summary": {"working": 0, "blocked": 0, "waiting_human": 0, "queued": 0},
              "active_runs": list(active_runs), "history": []}
    return Snapshot(status, {}, None, 0, 0, None, [], NOW, meter=meter or {})


def record(percent, reset, seen=NOW - timedelta(minutes=10)):
    return {"seen_at": seen.isoformat(), "windows": [{"used_percent": percent, "resets_at": reset.isoformat()}]}


def quota_left(snap, cfg, context):
    return usage._parts(snap, cfg, context)[1]


def line(snap, cfg, state=None):
    return " · ".join(usage._parts(snap, cfg, {"now": NOW, "state": state})[0])


def test_reset_shows_clock_within_a_day_and_date_after():
    soon, later = NOW + timedelta(hours=3), NOW + timedelta(days=3)
    text = line(snapshot({"usage": {"claude": record(6, soon), "codex": record(67, later)}}), config())
    assert f"Claude 6% (10 min ago · {soon.astimezone().strftime('%H:%M')})" in text
    local = later.astimezone()
    assert f"Codex 67% (10 min ago · {local.month}/{local.day} {local.strftime('%H:%M')})" in text


def test_highest_current_window_wins_and_reset_windows_do_not_count():
    claude = {"seen_at": NOW.isoformat(), "windows": [
        {"used_percent": 90, "resets_at": (NOW - timedelta(minutes=1)).isoformat()},
        {"used_percent": 30, "resets_at": (NOW + timedelta(hours=1)).isoformat()},
        {"used_percent": 12, "resets_at": (NOW + timedelta(hours=2)).isoformat()}]}
    assert "Claude 30%" in line(snapshot({"usage": {"claude": claude}}), config())


def test_all_reset_record_shows_dash_with_age_and_is_not_quota_left():
    snap = snapshot({"usage": {"claude": record(6, NOW - timedelta(hours=1))}})
    assert line(snap, config()).startswith("Claude — (10 min ago) · Codex —")
    assert usage.lines(snap, config(), {"now": NOW})[0] is None


def test_without_usage_still_shows_concurrency_and_mode():
    text = line(snapshot(), config())
    assert text == "Claude — · Codex — · running 0/2 · keep 80%"


def test_memory_word_only_when_not_normal():
    assert "memory" not in line(snapshot({"memory": "normal"}), config())
    assert "memory tight" in line(snapshot({"memory": "warn"}), config())
    assert "메모리 위험" in line(snapshot({"memory": "critical"}), config(language="ko"))


def test_burn_mode_and_explicit_list_keep_percent():
    assert line(snapshot({"mode": "burn"}), config()).endswith("burn")
    listed = config(executor_list=[{"name": "claude", "quota": "subscription", "keep_percent": 55}])
    assert line(snapshot({"mode": "keep"}), listed).endswith("keep 55%")
    bare = config(executor_list=[{"name": "codex", "quota": "subscription"}])
    assert line(snapshot({"mode": "keep"}), bare).endswith(" · keep")


def test_quota_left_uses_keep_cap_in_keep_and_100_otherwise():
    soon = NOW + timedelta(hours=1)
    at_cap = {"usage": {"claude": record(80, soon)}, "mode": "keep"}
    assert not quota_left(snapshot(at_cap), config(), {"now": NOW})
    assert quota_left(snapshot({**at_cap, "mode": "burn"}), config(), {"now": NOW})
    bare = config(executor_list=[{"name": "claude", "quota": "subscription"}])
    assert quota_left(snapshot(at_cap), bare, {"now": NOW}), "no keep_percent caps Claude at 100"
    assert quota_left(snapshot({"usage": {"codex": record(99, soon)}}), config(), {"now": NOW})
    assert not quota_left(snapshot({"usage": {"codex": record(100, soon)}}), config(), {"now": NOW})


def test_invite_needs_idle_lanes_and_sits_above_the_last_usage_line(tmp_path):
    left = {"usage": {"codex": record(10, NOW + timedelta(hours=1))}}
    rows = render_frame(snapshot(left), config(), tmp_path, width=200, now=NOW).splitlines()
    assert rows[-2] == "All lanes idle with quota left — type /fa to order work"
    assert rows[-1].startswith("Claude — · Codex 10%")
    busy = snapshot(left, active_runs=[{"number": 1, "lane": "x"}])
    assert usage.lines(busy, config(), {"now": NOW})[0] is None


def test_meter_reads_budget_from_max_parallel_then_machine_state_then_initial(store, monkeypatch):
    from foragent import capacity

    monkeypatch.setattr(capacity, "memory_pressure", lambda: "warn")
    assert read_meter(store, config(), NOW)["budget"] == capacity.INITIAL_PARALLEL
    capacity.write_state(store, 5, NOW)
    found = read_meter(store, config(), NOW)
    assert (found["budget"], found["memory"], found["running"], found["mode"]) == (5, "warn", 0, "keep")
    assert read_meter(store, config(max_parallel=3), NOW)["budget"] == 3
    (store.root / "usage.json").write_text(json.dumps({"claude": record(6, NOW + timedelta(hours=1))}))
    assert read_meter(store, config(), NOW)["usage"]["claude"]["windows"][0]["used_percent"] == 6


def test_m_flips_from_the_effective_mode_and_shows_it_before_the_next_refresh(store):
    cfg = config(claude_quota={"mode": "burn", "keep_percent": 80})
    state = {}
    handle_key("m", store, cfg, state=state)
    assert store.claude_quota_mode() == "keep"
    snap = replace(snapshot({"mode": "burn"}), captured_at=NOW)
    assert line(snap, cfg, state).endswith("keep 80%")
    handle_key("m", store, cfg, state=state)
    assert store.claude_quota_mode() == "burn"
    # An older press does not hide a later refresh.
    later = replace(snapshot({"mode": "keep"}), captured_at=datetime.now(UTC) + timedelta(seconds=5))
    assert line(later, cfg, state).endswith("keep 80%")


def test_m_flips_also_with_an_explicit_executors_list(store):
    cfg = config(executor_list=[{"name": "claude", "quota": "subscription", "keep_percent": 70}])
    handle_key("m", store, cfg)
    assert store.claude_quota_mode() == "burn"


def test_m_is_ignored_while_an_issue_box_is_open(store):
    state = {"issue": 7}
    handle_key("m", store, config(), state=state)
    assert store.claude_quota_mode() is None and "quota_mode" not in state


def test_help_line_lists_m(tmp_path):
    rows = render_frame(snapshot(), config(), tmp_path, width=200, now=NOW).splitlines()
    assert "m keep/burn" in rows[-2]


def test_short_screen_joins_the_usage_line_onto_the_key_letters(tmp_path):
    left = {"usage": {"codex": record(10, NOW + timedelta(hours=1))}}
    short = help_section.render(snapshot(left), config(), tmp_path, now=NOW, short=True)
    assert [[value for value, _ in row] for row in short] == [
        ["All lanes idle with quota left — type /fa to order work"],
        ["j/k move · Enter open · f fold · a all · o orchestrator · m keep/burn · x close · q quit · "
         "Claude — · Codex 10% (10 min ago · " + (NOW + timedelta(hours=1)).astimezone().strftime("%H:%M")
         + ") · running 0/2 · keep 80%"]]
    assert usage.render(snapshot(left), config(), tmp_path, now=NOW, short=True) == []


def test_usage_line_sits_under_help_and_above_the_separate_other_gates(tmp_path):
    snap = snapshot()
    snap.status["installed_gates"] = [{"repo": "o/other", "last_tick": None, "stale": False,
                                       "running_runs": 0, "current": False}]
    rows = render_frame(snap, config(), tmp_path, width=200, now=NOW).splitlines()
    assert "q quit" in rows[-4] and rows[-3].startswith("Claude —")
    assert rows[-2] == "" and "o/other" in rows[-1], "the other gates keep their own section"
    plain = render_frame(snapshot(), config(), tmp_path, width=200, now=NOW).splitlines()
    assert "q quit" in plain[-2], "directly under the help line"


def test_meter_without_a_machine_state_folder_counts_this_repository(tmp_path):
    from foragent.state import StateStore

    found = read_meter(StateStore(tmp_path / "missing" / "repo"), config(), NOW)
    assert "running" not in found and found["usage"] == {}
    snap = snapshot(found, active_runs=[{"number": 1, "lane": "x"}])
    assert "running 1/2" in line(snap, config())
