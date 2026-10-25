"""One road row per running worker, numbered in start order (#477 rule, kept by #113's merge)."""

from foragent.watch_layout import _clones, _lane_name, _worker_rows


def status(*runs):
    return {"active_runs": [{"lane": "orch", "number": number, "started_at": started} for number, started in runs]}


def lane_row(issue=None, state="working"):
    return {"slug": "orch", "lane_display": "🧭 orch", "issue": issue, "state": state}


def test_workers_are_numbered_by_start_time_then_issue_number():
    clones = _clones(status((9, "2026-10-25T10:00:00"), (3, "2026-10-25T11:00:00"), (5, "2026-10-25T10:00:00")))
    assert _worker_rows(lane_row(9), clones) == [("🧭 orch 1", [5]), ("🧭 orch 2", [9]), ("🧭 orch 3", [3])]
    assert _lane_name(lane_row(9), clones) == "🧭 orch"


def test_single_worker_keeps_the_plain_name_and_blocked_issue_stays_on_first_row():
    one = _clones(status((1, "2026-10-25T10:00:00")))
    assert _worker_rows(lane_row(1), one) == [("🧭 orch", [1])]
    two = _clones(status((1, "2026-10-25T10:00:00"), (2, "2026-10-25T10:05:00")))
    assert _worker_rows(lane_row(7, "blocked"), two) == [("🧭 orch 1", [7, 1]), ("🧭 orch 2", [2])]
