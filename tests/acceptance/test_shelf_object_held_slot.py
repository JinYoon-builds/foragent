"""Acceptance test for shelf-object: a closed issue whose run still holds a file-overlap slot.

The slot release itself is on main (#2152, test_closed_issue_stops_its_run); this issue adds the
shelf-side result: the held run is counted once in the held-runs place of `fa brief --json` and
leaves it in the same tick. Real tick (`scheduler.tick`, as the locked #2152 test drives it), real
state store and Git history; the tracker and the process table are the project's fakes, so no
real signal is sent. This file is separate because the fake process table is an autouse fixture.
"""

from datetime import timedelta

from shelf_object_support import brief, shelf_row
from test_closed_issue_stops_its_run import _start_other_open_run
from test_orch_stop import _case as stop_case
from test_stop_sees_running import gate_status
from test_stop_whole_tree import fake_processes, stop_context  # noqa: F401 (autouse fixture)


def test_closed_issue_holding_a_slot_is_counted_in_held_runs_and_leaves_in_the_same_tick(
    git_history, tmp_path, monkeypatch, fake_processes, capsys,  # noqa: F811
):
    from foragent import scheduler

    case, _tree, main = stop_case(git_history, tmp_path, monkeypatch, fake_processes)
    case.main = main
    _start_other_open_run(case, fake_processes)
    case.clock[0] += timedelta(minutes=1)
    case.tracker.close_issue(1, "Merged by hand in a direct PR.")
    for tick in range(2):
        case.clock[0] += timedelta(minutes=1)
        ctx = stop_context(case, fake_processes, start_runs=True)
        ctx.launcher = lambda number: 47000 + number
        scheduler.tick(ctx)
        active = {row["number"] for row in gate_status(main, capsys)["active_runs"]}
        assert 1 not in active and 2 in active, f"The closed issue's slot is released within one tick: {active}"
        row = shelf_row(brief(case, capsys), "held-runs")
        assert (row["count"], row["in_24h"], row["out_24h"]) == (0, 1, 1), (
            f"Tick {tick + 1}: the closed issue's run was counted once in held runs and left it in the same "
            f"tick; the open running issue is not held: {row}")
