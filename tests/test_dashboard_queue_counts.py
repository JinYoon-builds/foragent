"""Regressions for #2824 beyond its Done-when file: startable counting edge cases and the
planning-request list a click on the request count opens."""

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from test_queued_startable import _busy_snapshot, _box, _queue_case  # noqa: E402
from test_skills_autosync import isolated_home  # noqa: E402,F401
from test_squirrel_running import plain_frame, utf8_terminal, view_offline  # noqa: E402,F401


def _published(case, main, capsys):
    capsys.readouterr()
    assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    capsys.readouterr()
    return json.loads((case.store.root / "now.json").read_text(encoding="utf-8"))["summary"]


def test_an_open_dependency_keeps_approved_ready_work_from_counting_as_startable(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = _queue_case(git_history, tmp_path, monkeypatch)
    case.tracker.add_issue(11, "Waits for eight", labels=["approved", "ready", "lane:web"])
    case.tracker.add_blocked_by(11, 8)  # #8 is still open.
    assert main(["gate", "status", "--live", "--json", "--repo-root", str(case.root)]) == 0
    summary = _published(case, main, capsys)
    assert summary["startable"] == 1, summary  # Still only #2.
    assert summary["no_approval"] == 2, summary


def test_unapproved_ready_work_held_for_a_person_is_not_counted_as_without_approval(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main = _queue_case(git_history, tmp_path, monkeypatch)
    case.tracker.add_issue(12, "Asks twelve", labels=["ready", "needs-human", "lane:api"])
    assert main(["gate", "status", "--live", "--json", "--repo-root", str(case.root)]) == 0
    summary = _published(case, main, capsys)
    assert summary["no_approval"] == 2, "a question holds #12 first; only #9 and #10 lack approval"


def test_unreadable_ready_records_make_both_counts_unknown_never_zero(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent import watch_now

    case, main = _queue_case(git_history, tmp_path, monkeypatch)

    def unreadable(*_, **__):
        raise OSError("flow log unreadable")

    monkeypatch.setattr(case.store, "latest_flow_records", unreadable)
    counts = watch_now.startable_counts(case.store, {"active_runs": []})
    assert counts == {"startable": None, "no_approval": None}


def _click(snapshot, config, root, state, target):
    from foragent import watch

    layout = {}
    watch.render_frame(snapshot, config, root, width=240, state=state, layout=layout)
    hits = [hit for hit in layout["hits"] if hit[-1] == target]
    assert len(hits) == 1, f"one {target} target, got {layout['hits']}"
    x0, y0, *_ = hits[0]
    watch.handle_mouse("left", x0, y0, layout, None, config, state=state)
    return watch.render_frame(snapshot, config, root, width=240, state=state)


def test_clicking_the_request_count_lists_each_open_request_in_the_bubble(tmp_path):
    from foragent.watch_sections.lane_bubble import REQUESTS_TARGET

    snapshot, config = _busy_snapshot("ko")
    state = {}
    frame = _click(snapshot, config, tmp_path, state, REQUESTS_TARGET)
    rows = _box(frame)
    assert rows[:3] == ["기획 세션 요청 2개", "· Plan the login order", "· Tidy the readme order"], rows
    assert "An old finished request" not in frame and "Split watch_layout" not in frame


def test_the_request_count_has_no_click_target_without_open_requests(tmp_path):
    from foragent import watch
    from foragent.watch_sections.lane_bubble import REQUESTS_TARGET

    snapshot, config = _busy_snapshot("en", requests=False)
    layout = {}
    watch.render_frame(snapshot, config, tmp_path, width=240, layout=layout)
    assert not [hit for hit in layout["hits"] if hit[-1] == REQUESTS_TARGET]


def test_the_live_request_list_comes_from_the_published_snapshot_without_reading_the_box(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from foragent import orch_inbox
    from foragent.watch_sections.lane_bubble import REQUESTS_TARGET

    snapshot, config = _busy_snapshot("en")
    snapshot = replace(snapshot, requests=[], now_view={
        "lanes": {}, "summary": {"requests": ["Plan the login order", "Tidy the readme order"]}})
    reader = Mock(side_effect=AssertionError("the click must not read the box"))
    monkeypatch.setattr(orch_inbox, "requests", reader)
    frame = _click(snapshot, config, tmp_path, {"store": object()}, REQUESTS_TARGET)
    assert "Plan the login order" in frame and "Tidy the readme order" in frame
    assert reader.call_count == 0


# ---------------------------------------------------------------- the estimate follows the holds

from test_eta_remaining_work import add_work, eta_case, snapshot  # noqa: E402,F401
from test_order_progress import isolated_home as _order_home  # noqa: E402,F401


def _hours(case, found):
    return None if found.eta is None else (found.eta - case.clock[0]).total_seconds() / 3600


def _ready_child(case, number, size):
    add_work(case, number, size)
    case.tracker.add_labels(number, ["ready"])


def test_a_gate_hold_this_tick_takes_its_issue_out_of_the_finish_estimate(eta_case):
    """The review's case: an approved+ready child; a file-overlap hold takes it out again.

    The fixture's #1 runs in the same order, so the estimate without the child is its own.
    """
    case = eta_case
    running_only = snapshot(case)
    _ready_child(case, 10, "m")
    with_child = snapshot(case)
    assert _hours(case, with_child) > _hours(case, running_only)
    case.store.record_flow("ready", issue=10, run_started_at=None, reason="file-overlap", outcome="waiting")
    held = snapshot(case)
    assert (held.eta, held.eta_range) == (running_only.eta, running_only.eta_range), (
        "held work is not startable, so the estimate drops it and both bounds move")


@pytest.mark.parametrize("hold", ["file-overlap", "slots-full", "parked", "blocked", "needs-human"])
def test_every_recorded_hold_leaves_the_estimate_and_the_rest_stays(eta_case, hold):
    case = eta_case
    _ready_child(case, 10, "m")
    one = _hours(case, snapshot(case))
    _ready_child(case, 11, "m")
    both = _hours(case, snapshot(case))
    assert both > one
    if hold in ("file-overlap", "slots-full"):
        case.store.record_flow("ready", issue=11, run_started_at=None, reason=hold, outcome="waiting")
    elif hold == "parked":
        from foragent import orch_events

        # The journal's park record holds it even before the approval label goes.
        orch_events.append_event(case.store, case.clock[0], 11, "parked", "#11: parked")
    else:
        case.tracker.add_labels(11, [hold])
    assert _hours(case, snapshot(case)) == one, hold


def test_a_started_record_puts_held_work_back_into_the_estimate(eta_case):
    case = eta_case
    _ready_child(case, 10, "m")
    counted = snapshot(case).eta
    case.store.record_flow("ready", issue=10, run_started_at=None, reason="file-overlap", outcome="waiting")
    assert snapshot(case).eta < counted
    case.store.record_flow("ready", issue=10, run_started_at="2026-10-25T00:00:00+00:00",
                           reason=None, outcome="started")
    assert snapshot(case).eta == counted


def test_a_live_run_stays_in_the_estimate_whatever_its_last_ready_record_says(eta_case):
    case = eta_case
    counted = snapshot(case).eta  # #1 runs.
    case.store.record_flow("ready", issue=1, run_started_at=None, reason="file-overlap", outcome="waiting")
    assert snapshot(case).eta == counted


def test_unreadable_ready_records_give_no_estimate_rather_than_counting_held_work(eta_case, monkeypatch):
    case = eta_case
    _ready_child(case, 10, "m")
    assert snapshot(case).eta is not None

    def unreadable(**_):
        raise OSError("flow log unreadable")

    from foragent.state import StateStore

    monkeypatch.setattr(StateStore, "latest_flow_records", lambda self, *_, **__: unreadable())  # Read afresh.
    assert snapshot(case).eta is None


def test_a_busy_flow_lock_makes_the_counts_unknown_without_aborting_the_ticks_later_locks(
    git_history, tmp_path, monkeypatch,
):
    """The counts are an optional observation inside the gate's tick (watch_now.publishing)."""
    from foragent import watch_now
    from foragent.file_locks import file_lock, reporting

    case, _ = _queue_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "0")
    with reporting(case.store.root):
        with file_lock(case.store.root / "flow.lock"):
            counts = watch_now.startable_counts(case.store, {"active_runs": []})
        assert counts == {"startable": None, "no_approval": None}
        # The tick goes on: a later lock in the same operation is still taken.
        with file_lock(case.store.root / "later.lock"):
            pass
