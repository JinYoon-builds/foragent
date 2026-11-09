"""The importer check stops on silence, not on a fixed total time (r1305, 11-09).

Public entry point: `fa order publish` through cli.main, with the real collection and importer check.
See importer_stall_case.py for the fixture, the check's clock and the stall window.
"""

import pytest

from importer_stall_case import (  # noqa: F401
    IMPORTER, QUIET_TEST, SHARED, SILENCE_WORDS, STEP_S, call, collection_case, set_stall_window, snapshot,
    stall_case, timed_call,
)
from test_publish_collect_check import git_command


def test_importers_longer_than_120_seconds_in_total_complete_while_they_keep_finishing_tests(stall_case, capsys):
    case = stall_case.build(steps=6)
    clock = case.use_check_clock()

    code, diagnostic = call(case, capsys, "publish", "--dry-run")
    assert code == 0, f"The importer check refused an order whose importers kept finishing tests: {diagnostic}"
    assert "did not finish within" not in diagnostic, diagnostic
    base, order = case.lines("base"), case.lines("order")
    # 6 steps, the value test and the quiet test, in each run.
    assert len(base) == 8 and len(order) == 8, f"Both importer runs must run every test: {base} {order}"
    assert clock.finished() * STEP_S > 120, "The runs must take more than 120 s of the check's clock"

    before = git_command(case.history.origin, "rev-parse", "main")
    code, diagnostic = call(case, capsys, "publish")
    assert code == 0, f"publish refused an order whose importers kept finishing tests: {diagnostic}"
    assert "did not finish within" not in diagnostic, diagnostic
    assert git_command(case.history.origin, "rev-parse", "main") != before, "The order was not published"
    assert git_command(case.history.origin, "show", f"main:{SHARED}") == \
        case.plan["issues"][0]["acceptance_files"][SHARED].strip()
    assert len(case.tracker.list_issues()) == 2


@pytest.mark.parametrize("silent_in", ["base", "order"])
def test_a_run_that_reports_no_finished_test_for_the_stall_window_is_stopped_and_named(
        stall_case, capsys, monkeypatch, silent_in):
    # The quiet test sleeps 40 s in one run without finishing; the stall window is 3 s.
    case = stall_case.build(silent_in=silent_in, steps=2, silent=40)
    set_stall_window(monkeypatch, 3.0)
    before, readers = snapshot(case), len(case.worker_calls)

    code, diagnostic, elapsed = timed_call(case, capsys, "publish", "--dry-run")
    assert code != 0, f"The check accepted an order although the {silent_in} run went silent: {diagnostic}"
    assert elapsed < 25, f"The check waited {elapsed:.0f} s for the silent run instead of stopping it"
    assert SILENCE_WORDS.search(diagnostic), f"The refusal must say the run reported no progress: {diagnostic}"
    assert "did not finish within" not in diagnostic, f"The refusal blames a total time limit: {diagnostic}"
    assert IMPORTER in diagnostic, f"The refusal must name the importing tests: {diagnostic}"
    if silent_in == "base":
        assert "base" in diagnostic.lower(), f"The refusal must say the base run went silent: {diagnostic}"
        assert "order's files" not in diagnostic, f"The base run went silent, not the order's: {diagnostic}"
    else:
        assert "order's files" in diagnostic, \
            f"The refusal must say the run with the order's files went silent: {diagnostic}"
    assert snapshot(case) == before, "A refusal changed publication files, index, origin, issues or waiting orders"
    assert len(case.worker_calls) == readers, "An importer refusal must precede the preread model"
    assert f"{silent_in} quiet" not in case.lines(silent_in), f"{QUIET_TEST} was waited out"
