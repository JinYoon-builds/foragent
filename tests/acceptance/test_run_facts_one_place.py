"""#1515: run detail, the latest run and blocked reasons each live in one place.

Architect survey r206 (2026-11-01) picked three duplicated facts that cost runs:
- D15: run detail was cut at 500 characters, so 25 retries-exceeded runs on 10-31
  recorded only the start of the pytest command and the cause never showed;
- D06: the latest run and its history were two separate atomic writes, so a crash
  between them leaves the two disagreeing;
D04 (blocked reasons in four places) has no failing observable case on main once
writes go through the gate's recording tracker; it is left to the order card.
"""

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path


from foragent import orch_scorecard
from foragent.state import StateStore

NOW = datetime(2026, 11, 3, 9, tzinfo=UTC)

# A detail shaped like the 10-31 records: the command and its node list first, the cause after it.
NODES = " ".join(f"tests/test_module_{i:03d}.py::test_case_{i:03d}" for i in range(40))
COMMAND = f"Command: PYTEST_ADDOPTS= uv run pytest {NODES}"
CAUSE = "Cause: the protected check timed out after 900 seconds"
LONG_DETAIL = f"Retries exceeded. {COMMAND}\n{CAUSE}"


def _store(tmp_path, at=NOW):
    return StateStore(tmp_path / "state", now=lambda: at)


def _start(store, number=1):
    return store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number))


def _history(store, run):
    return [entry for entry in store.read_history()
            if entry.number == run.number and entry.started_at == run.started_at]


# D15: the run detail keeps its cause.

def test_run_detail_keeps_the_cause_after_a_long_command_in_latest_run_and_history(tmp_path):
    assert len(COMMAND) > 1000
    store = _store(tmp_path)
    run = _start(store)
    store.finish_run(1, "retries-exceeded", LONG_DETAIL)
    reopened = StateStore(store.root)
    assert CAUSE in reopened.get_run(1).detail
    entry, = _history(reopened, run)
    assert CAUSE in entry.detail

    # A detail saved while running is the one kept when the run finishes without a new one.
    store = _store(tmp_path / "second")
    run = _start(store)
    store.update_run(1, detail=LONG_DETAIL)
    assert CAUSE in store.get_run(1).detail
    store.finish_run(1, "retries-exceeded")
    reopened = StateStore(store.root)
    assert CAUSE in reopened.get_run(1).detail
    entry, = _history(reopened, run)
    assert CAUSE in entry.detail


def test_scorecard_repeated_failure_line_shows_the_cause(tmp_path):
    clock = {"now": NOW - timedelta(hours=2)}
    store = StateStore(tmp_path / "state", now=lambda: clock["now"])
    for number in (1006, 1158, 1185):
        _start(store, number)
        clock["now"] += timedelta(minutes=10)
        store.finish_run(number, "retries-exceeded", LONG_DETAIL)
    clock["now"] = NOW
    card = orch_scorecard.collect(store, days=2)
    repeated = card["repeated_failures"]
    assert repeated, card
    assert any(CAUSE in item["reason"] and item["count"] == 3 for item in repeated), repeated


# D06: the latest run and its history cannot split.

def test_a_crash_while_finishing_leaves_latest_run_and_history_agreeing(tmp_path, monkeypatch):
    """Whichever state write the crash hits, a reader never sees a finished history beside a running latest run."""
    real_replace = os.replace
    failing_write = 0
    while True:
        failing_write += 1
        assert failing_write < 20, "finish_run must not need this many state writes"
        store = _store(tmp_path / f"crash-{failing_write}")
        run = _start(store)
        root = store.root.resolve().as_posix()
        writes = []

        def replace(source, destination, *args, **kwargs):
            if Path(os.fspath(destination)).resolve().as_posix().startswith(root + "/"):
                writes.append(destination)
                if len(writes) == failing_write:
                    raise OSError("simulated crash between run writes")
            return real_replace(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace)
        crashed = False
        try:
            store.finish_run(1, "retries-exceeded", "Retries exceeded.")
        except OSError:
            crashed = True
        finally:
            monkeypatch.setattr(os, "replace", real_replace)

        reopened = StateStore(store.root)
        latest, history = reopened.get_run(1), _history(reopened, run)
        assert latest is not None, failing_write
        observed = (failing_write, latest.status, [entry.outcome for entry in history])
        if latest.status == "finished":
            assert history and history[-1].outcome == latest.outcome == "retries-exceeded", observed
        else:
            assert history == [], observed
        if not crashed:
            break
