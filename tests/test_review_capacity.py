"""Review admission applies to every store and every panel size (#1169)."""

from datetime import UTC, datetime
import os
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from foragent import capacity, capacity_sessions, review_perspectives
from foragent.executors import ExecResult
from foragent.issue import parse_body
from foragent.run_review import _panel_review
from runner_support import BODY, review_context  # noqa: F401


@pytest.mark.parametrize("machine_store", [True, False], ids=["machine-store", "other-store"])
@pytest.mark.parametrize("workers", [1, 4], ids=["one-worker", "many-workers"])
@pytest.mark.parametrize("fails", [False, True], ids=["pass", "review-error"])
@pytest.mark.parametrize("explicit_budget", [False, True], ids=["default-budget", "saved-budget"])
def test_every_reviewer_holds_capacity_and_releases_it(
    review_context, tmp_path, monkeypatch, machine_store, workers, fails, explicit_budget,
):
    ctx = review_context
    monkeypatch.setattr(capacity, "machine_root", lambda: (
        ctx.store.root.parent if machine_store else tmp_path / "other-machine"))
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.0))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    if explicit_budget:
        capacity.write_state(ctx.store, 2, datetime.now(UTC))
    else:
        assert capacity.read_state(ctx.store) is None
    if workers == 1:
        # A serial panel cannot assume a parent run still owns its slot.
        ctx.store.finish_run(1, "finished")
    else:
        ctx.store.update_run(1, pid=os.getpid())
    shares = review_perspectives.shares(ctx.config)
    overlap = min(workers, 2)
    barrier, lock = Barrier(overlap, timeout=5), Lock()
    calls, errors = [], []
    active = maximum = completed = 0

    def execute(**kwargs):
        nonlocal active, maximum, completed
        with lock:
            index = len(calls)
            calls.append(kwargs)
            records = capacity_sessions.read(ctx.store.root)
            if index == len(shares):
                assert completed == len(shares) and active == 0
                assert records == [], "Merge follows release of every reviewer slot"
                return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])
            active += 1
            maximum = max(maximum, active)
            holders = capacity.machine_runs(ctx.store, datetime.now(UTC))
            if not active <= len(records) <= holders <= 2:
                errors.append((active, len(records), holders))
        try:
            barrier.wait()
            if fails and index == 0:
                raise RuntimeError("review failed")
            return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])
        finally:
            with lock:
                active -= 1
                completed += 1

    ctx.executor_factory = lambda name: SimpleNamespace(run=execute)
    result = _panel_review(1, ctx, ctx.git, parse_body(BODY), "origin/main", "codex",
                           "", "Risk notes", shares, 1, workers=workers)
    assert not errors, f"Uncounted or over-budget reviewers: {errors}"
    assert maximum == overlap
    assert completed == len(shares), "An error never drops queued perspectives"
    assert len(calls) == len(shares) + (not fails)
    assert result.verdict == ("UNUSABLE" if fails else "PASS")
    assert capacity_sessions.read(ctx.store.root) == []
