"""Acceptance test for outbox-hold-scoped: an undelivered GitHub write holds only its own issue."""

from dataclasses import replace
from datetime import timedelta

import pytest

from foragent import scheduler
from foragent.capacity import write_state
from test_scheduler import BODY, NOW, approve, ctx  # noqa: F401


@pytest.fixture
def lane(ctx):  # noqa: F811
    """A gate whose launcher only records starts; no process is spawned or signalled."""
    ctx.config = replace(ctx.config, max_parallel=None, skill_libraries=[],
                         lanes={"orch": {"paths": ["src/foragent/**"]}})
    ctx.start_runs = True
    clock = [NOW]
    ctx.now = lambda: clock[0]
    ctx.starts = []

    def launch(number=None, *, kind="issue", lane=None):
        if kind == "issue":
            ctx.starts.append(number)
            return 7000 + number
        return None

    ctx.launcher = launch
    ctx.advance = lambda: clock.__setitem__(0, clock[0] + timedelta(seconds=1))
    write_state(ctx.store, 5, ctx.now())
    return ctx


def _add(context, number):
    body = BODY + f"\n## Files\n- src/f{number}.py\n"
    approve(context, number, body=body, labels=["lane:orch", "ready"])


def _reasons(context, number):
    return [row["data"]["reason"] for row in context.store.flow_records()
            if row["kind"] == "ready" and row["issue"] == number]


def _tick(context):
    report = scheduler.tick(context)
    context.advance()
    return report


def _settle(context):
    """One pass with starts off: approval snapshots and their comments are delivered first,
    so only the write the test queues afterwards is pending when the gate pass begins."""
    context.start_runs = False
    _tick(context)
    context.start_runs = True
    assert context.starts == [] and context.store.github_blocking_outbox() == []


def _pending_for(store, number):
    return [entry for entry in store.github_blocking_outbox() if entry["issue"] == number]


@pytest.mark.parametrize("held,free", [(1, 2), (2, 1)], ids=["held-ranked-first", "held-ranked-last"])
def test_undelivered_write_holds_only_its_own_issue_then_releases_it(lane, held, free):
    _add(lane, held)
    _add(lane, free)
    _settle(lane)
    deliver = lane.tracker.flush_outbox
    lane.tracker.flush_outbox = lambda: None  # Keep A's workflow write undelivered through this pass.
    lane.store.queue_github_write(held, "add_labels", labels=["keep"])
    assert _pending_for(lane.store, held) and not _pending_for(lane.store, free)

    _tick(lane)
    # B (no pending write) starts in the same pass, exactly once; A waits on its own write.
    assert lane.starts == [free], f"expected only #{free} to start, got {lane.starts}"
    assert "outbox-wait" not in _reasons(lane, free)
    assert _reasons(lane, held)[-1:] == ["outbox-wait"]

    # A's write is delivered; the next pass starts A, and B is not started again.
    lane.tracker.flush_outbox = deliver
    deliver()
    assert not _pending_for(lane.store, held)
    _tick(lane)
    assert lane.starts == [free, held], f"expected #{held} to start after delivery, got {lane.starts}"


def test_issue_with_pending_write_does_not_start_while_others_do(lane):
    for number in (1, 2, 3):
        _add(lane, number)
    _settle(lane)
    lane.tracker.flush_outbox = lambda: None
    lane.store.queue_github_write(2, "add_labels", labels=["keep"])
    _tick(lane)
    assert sorted(lane.starts) == [1, 3]
    assert len(lane.starts) == len(set(lane.starts))
    assert 2 not in lane.starts
