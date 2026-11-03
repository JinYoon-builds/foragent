"""Locked acceptance for merge-turn-ready-first. Written before implementation.

The 2026-11-03 shape: the run that holds the merge turn finds main moved, so it
must refresh onto the new main and wait for CI again. A second run, verified on
that same new main with green CI, is ready to merge now. It must merge while the
holder's new CI is still running, and the holder must still merge afterwards.

Break-it: a turn that stays with a holder busy refreshing or waiting for CI makes
the ready run wait for the holder's CI and fails the first check. A holder that
loses its run, or is blocked or flagged instead of merging after the ready run,
fails the second.
"""

from concurrent.futures import ThreadPoolExecutor
import time
from threading import Event, Lock

from conftest import git_command
from test_base_move_reuse import REVERIFY, publish_case  # noqa: F401
from test_merge_turn import assert_clean_merge, turn_case  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


HOLDER, READY = 5, 1  # The holder joins first; the ready run joins later.
WAIT = 30  # Real seconds; the fake CI answers at once when it is released.


def wait_until(predicate, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_ready_current_run_merges_while_the_turn_holder_refreshes_and_waits_for_ci(turn_case, monkeypatch):
    case = turn_case
    checks = case.tracker.pr_checks
    lock = Lock()
    reached_ci = {n: Event() for n in (HOLDER, READY)}
    first_green = {n: Event() for n in (HOLDER, READY)}
    holder_in_second_ci = Event()
    release_holder_ci = Event()
    holder_ci_polls_after_refresh = []

    def poll(pr):
        number = case.worker.number
        if not reached_ci[number].is_set():
            reached_ci[number].set()
            assert first_green[number].wait(WAIT), f"Run {number} never received its first green CI"
        if number == HOLDER and REVERIFY in case.store.log_path(HOLDER).read_text():
            # The holder refreshed onto the moved main: its new CI runs until released.
            with lock:
                holder_ci_polls_after_refresh.append(pr)
            holder_in_second_ci.set()
            assert release_holder_ci.wait(WAIT * 3), "The holder's second CI was never released"
        return checks(pr)

    def sleep(seconds):
        # Queue waits really pass time; nothing here decides who merges.
        time.sleep(0.02)

    case.ctx.sleep = sleep
    monkeypatch.setattr(case.tracker, "pr_checks", poll)
    ready_merged_during_holder_ci = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        try:
            futures[HOLDER] = pool.submit(case.run, HOLDER)
            assert reached_ci[HOLDER].wait(WAIT), "The holder did not reach CI"
            # Main moves with a result-affecting source change after the holder verified.
            case.history.update({"src/moved_main.py": "VALUE = 'moved'\n"})
            moved_main = case.history.tip
            # The ready run starts on the moved main, so its verification is current.
            futures[READY] = pool.submit(case.run, READY)
            assert reached_ci[READY].wait(WAIT), "The ready run did not reach CI"
            first_green[HOLDER].set()
            assert holder_in_second_ci.wait(WAIT), (
                "The holder must take the turn, refresh onto the moved main and wait for CI again"
            )
            first_green[READY].set()
            ready_merged_during_holder_ci = wait_until(
                lambda: any(row["n"] == READY for row in case.merges)
            )
            holder_still_in_ci = not release_holder_ci.is_set()
        finally:
            for gate in first_green.values():
                gate.set()
            release_holder_ci.set()
        outcomes = {n: future.result(timeout=WAIT * 4) for n, future in futures.items()}

    logs = {n: case.store.log_path(n).read_text() for n in (HOLDER, READY)}
    assert ready_merged_during_holder_ci and holder_still_in_ci, (
        "A run that is current and green must merge while the turn holder refreshes and waits "
        f"for its own CI; merges so far: {[row['n'] for row in case.merges]}\n"
        f"Ready run log:\n{logs[READY]}"
    )
    assert REVERIFY not in logs[READY], "The ready run was verified on the moved main already"
    ready_merge = next(row for row in case.merges if row["n"] == READY)
    assert ready_merge["base"] == moved_main, "The ready run merges onto the main it verified"
    assert holder_ci_polls_after_refresh, "The holder must have been in its second CI"

    assert_clean_merge(case, READY, outcomes[READY])
    assert_clean_merge(case, HOLDER, outcomes[HOLDER])
    assert [row["n"] for row in case.merges] == [READY, HOLDER], (
        "The holder keeps its place and merges right after the ready run"
    )
    holder_merge = case.merges[-1]
    git_command(case.history.origin, "merge-base", "--is-ancestor", ready_merge["head"], holder_merge["head"])
    paths = git_command(case.history.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert {f"src/turn_{HOLDER}.py", f"src/turn_{READY}.py", "src/moved_main.py"} <= set(paths), (
        "Main must hold the moved change and both runs' source changes"
    )
