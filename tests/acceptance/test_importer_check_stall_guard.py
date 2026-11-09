"""Regression controls for the importer check's stall window (r1305, 11-09); outside Done when.

- A test that passes on the base and fails with the order's files still refuses the order, both for
  quick importers and for importers that take over 120 s of the check's clock.
- A run that keeps finishing tests for longer than the stall window is not stopped.
- A run that goes silent after some progress is still stopped.
"""

from importer_stall_case import (  # noqa: F401
    BROKEN_TEST, IMPORTER, QUIET_TEST, SILENCE_WORDS, STEP_S, call, collection_case, set_stall_window,
    snapshot, stall_case, timed_call,
)


def assert_broken_refusal(case, capsys):
    before, readers = snapshot(case), len(case.worker_calls)
    code, diagnostic = call(case, capsys, "publish", "--dry-run")
    assert code != 0, f"An order that breaks {BROKEN_TEST}, which passes on the base, was accepted: {diagnostic}"
    assert IMPORTER in diagnostic and BROKEN_TEST in diagnostic, \
        f"The refusal must name the broken importer test: {diagnostic}"
    assert "the order broke what this importer needs" in diagnostic, f"The refusal must say why: {diagnostic}"
    assert "test_keeps_reporting" not in diagnostic, f"Only the broken test is named: {diagnostic}"
    assert snapshot(case) == before
    assert len(case.worker_calls) == readers
    return diagnostic


def test_breaking_change_with_quick_importers_is_still_refused(stall_case, capsys):
    assert_broken_refusal(stall_case.build(broken=True, steps=2), capsys)


def test_breaking_change_with_importers_over_120_seconds_is_refused_for_the_broken_test(stall_case, capsys):
    case = stall_case.build(broken=True, steps=6)
    clock = case.use_check_clock()
    diagnostic = assert_broken_refusal(case, capsys)
    assert "did not finish within" not in diagnostic, f"The refusal blames a total time limit: {diagnostic}"
    assert clock.finished() * STEP_S > 120, "The runs must take more than 120 s of the check's clock"


def test_a_run_that_keeps_finishing_tests_for_longer_than_the_stall_window_completes(
        stall_case, capsys, monkeypatch):
    # Each test takes about 0.6 s and each run about 7 s, longer than the 4 s window.
    case = stall_case.build(steps=12, pause=0.2)
    set_stall_window(monkeypatch, 4.0)
    code, diagnostic = call(case, capsys, "publish", "--dry-run")
    assert code == 0, f"A run that kept finishing tests was stopped: {diagnostic}"
    assert len(case.lines("base")) == 14 and len(case.lines("order")) == 14, case.progress.read_text()


def test_a_run_that_goes_silent_after_progress_is_still_stopped(stall_case, capsys, monkeypatch):
    case = stall_case.build(silent_in="order", steps=6, pause=0.2, silent=40)
    set_stall_window(monkeypatch, 4.0)
    code, diagnostic, elapsed = timed_call(case, capsys, "publish", "--dry-run")
    assert code != 0, f"The check accepted an order although its run went silent: {diagnostic}"
    assert elapsed < 30, f"The check waited {elapsed:.0f} s for the silent run instead of stopping it"
    assert SILENCE_WORDS.search(diagnostic) and "order's files" in diagnostic, diagnostic
    assert len(case.lines("order")) == 7, "The progress before the silence was not run"
    assert "order quiet" not in case.lines("order"), f"{QUIET_TEST} was waited out"
