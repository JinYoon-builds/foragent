"""The public workload context owns machine/test acquisition and release."""

from datetime import UTC, datetime

import pytest

from foragent import capacity
from foragent.state import StateStore
from test_order_machine_budget import fake_workloads, planning  # noqa: F401


def workload():
    hold = getattr(capacity, "hold_workload", None)
    assert callable(hold), "Capacity needs one shared acquire/release workload context"
    return hold


@pytest.mark.parametrize("exception", [None, RuntimeError, KeyboardInterrupt])
def test_workload_counts_once_and_releases_on_every_exit(planning, exception):
    case, hold = planning, workload()
    with pytest.raises(exception) if exception else __import__("contextlib").nullcontext():
        with hold(case.store, case.config):
            assert capacity.machine_runs(case.store, datetime.now(UTC)) == 1
            # Synchronous nesting reuses its owning lease.
            with hold(case.store, case.config):
                assert capacity.machine_runs(case.store, datetime.now(UTC)) == 1
            if exception:
                raise exception("offline cancellation")
    assert capacity.machine_runs(case.store, datetime.now(UTC)) == 0


@pytest.mark.parametrize("reason", ["full", "overload", "critical"])
def test_workload_waits_on_existing_sensors_and_releases_sibling_capacity(planning, reason, monkeypatch, capsys):
    case, hold = planning, workload()
    sibling = StateStore(case.store.root.parent / "other")
    blocked = [True]
    if reason == "full":
        sibling.start_run(99, branch="other", worktree="other", pid=capacity.os.getpid())
    monkeypatch.setattr(capacity, "machine_load", lambda: (
        "normal", capacity.OVERLOAD_LOAD if blocked[0] and reason == "overload" else .5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "critical" if blocked[0] and reason == "critical" else "normal")
    waits = []

    def release(seconds):
        waits.append(seconds)
        blocked[0] = False
        if sibling.active_runs():
            sibling.finish_run(99, "merged")

    monkeypatch.setattr(capacity.time, "sleep", release)
    with hold(case.store, case.config):
        assert waits and capacity.machine_runs(case.store, datetime.now(UTC)) == 1
    assert capacity.machine_runs(case.store, datetime.now(UTC)) == 0
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1 and "wait" in lines[0].lower()


def test_nonblocking_workload_defers_without_sleep_or_leak(planning, monkeypatch):
    case, hold = planning, workload()
    sibling = StateStore(case.store.root.parent / "other")
    sibling.start_run(99, branch="other", worktree="other", pid=capacity.os.getpid())
    monkeypatch.setattr(capacity.time, "sleep", lambda _: pytest.fail("Tick admission cannot sleep"))
    with pytest.raises(TimeoutError):
        with hold(case.store, case.config, wait_s=0):
            pytest.fail("A full machine must defer optional work")
    assert capacity.machine_runs(case.store, datetime.now(UTC)) == 1


@pytest.mark.parametrize("language", ["en", "ko"])
def test_wait_names_holders_and_cancellation_in_selected_language(planning, monkeypatch, capsys, language):
    from dataclasses import replace
    case, hold = planning, workload()
    case.config = replace(case.config, language=language)
    sibling = StateStore(case.store.root.parent / "other")
    sibling.start_run(99, branch="other", worktree="other", pid=capacity.os.getpid())
    def release(_):
        sibling.finish_run(99, "merged")
    monkeypatch.setattr(capacity.time, "sleep", release)
    with hold(case.store, case.config):
        pass
    err = capsys.readouterr().err
    assert "other" in err and "99" in err and "Ctrl-C" in err
    assert "1" in err and "0" in err, "Show budget/used/free quantities"
    if language == "ko":
        assert any("가" <= ch <= "힣" for ch in err), "Wait guidance must use selected i18n locale"
