"""Locked acceptance for merge-turn-priority. Written before implementation.

Break-it: FIFO-only, issue-number sorting, treating every listed issue equally,
ignoring a later ready run, caching the priority order, or granting an empty
queue to a withdrawn run must fail these public-entry-point checks. Constant
True/False answers also fail because each observation checks every contender.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from foragent import orch_queue, run_merge_turn as turn
from foragent.state import StateStore


NOW = datetime(2026, 11, 1, 9, tzinfo=UTC)
A, B, C = 4102, 4101, 4103  # Join order deliberately differs from issue order.


class FakeStore(StateStore):
    """Real isolated queue storage, with only run liveness supplied in memory."""

    def __init__(self, root, numbers):
        super().__init__(root, now=lambda: NOW)
        self.runs = {
            n: SimpleNamespace(number=n, started_at=NOW.isoformat(), status="running")
            for n in numbers
        }

    def get_run(self, number):
        return self.runs.get(number)


def make_queue(tmp_path, *numbers):
    store = FakeStore(tmp_path / "state", numbers)
    ctx = SimpleNamespace(store=store, now=lambda: NOW)
    seats = {n: turn.seat(ctx, n, "main") for n in numbers}
    return ctx, seats


def assert_holder(store, seats, expected):
    actual = [place.n for place in seats.values() if turn.holds_turn(store, place)]
    wanted = [] if expected is None else [expected]
    assert actual == wanted, f"Expected merge-turn holders {wanted}, got {actual}"


@pytest.mark.parametrize(
    ("priority", "expected"),
    [([B], B), ([], A), ([B, A], B), ([A, B], A)],
    ids=["listed-later", "empty-list-fifo", "list-order-beats-fifo", "listed-first"],
)
def test_ready_runs_follow_global_list_then_join_order(tmp_path, priority, expected):
    ctx, seats = make_queue(tmp_path, A, B)
    orch_queue.set_global(ctx.store, priority)
    turn.join(ctx, seats[A], "head-a")
    turn.join(ctx, seats[B], "head-b")

    assert_holder(ctx.store, seats, expected)


def test_listed_run_joining_later_precedes_earlier_unlisted_runs(tmp_path):
    ctx, seats = make_queue(tmp_path, A, B, C)
    orch_queue.set_global(ctx.store, [C])
    turn.join(ctx, seats[A], "head-a")
    turn.join(ctx, seats[B], "head-b")
    # A listed live run that has not joined is not ready and cannot take a turn.
    assert_holder(ctx.store, seats, A)

    turn.join(ctx, seats[C], "head-c")
    assert_holder(ctx.store, seats, C)
    turn.withdraw(ctx, seats[C])
    assert_holder(ctx.store, seats, A)
    turn.withdraw(ctx, seats[A])
    assert_holder(ctx.store, seats, B)


def test_existing_ready_runs_follow_the_current_priority_without_rejoining(tmp_path):
    ctx, seats = make_queue(tmp_path, A, B)
    turn.join(ctx, seats[A], "head-a")
    turn.join(ctx, seats[B], "head-b")

    for priority, expected in [([], A), ([B], B), ([A, B], A), ([], A)]:
        orch_queue.set_global(ctx.store, priority)
        before = {p.relative_to(ctx.store.root): p.read_bytes()
                  for p in ctx.store.root.rglob("*") if p.is_file()}
        assert_holder(ctx.store, seats, expected)
        after = {p.relative_to(ctx.store.root): p.read_bytes()
                 for p in ctx.store.root.rglob("*") if p.is_file()}
        assert after == before, "Reading the derived turn must not write queue state"


@pytest.mark.parametrize(
    ("withdrawn", "expected"),
    [((A,), B), ((B,), A), ((A, B), None)],
    ids=["earlier-withdrawn", "listed-withdrawn", "all-withdrawn"],
)
def test_withdrawn_runs_never_hold_the_turn(tmp_path, withdrawn, expected):
    ctx, seats = make_queue(tmp_path, A, B)
    orch_queue.set_global(ctx.store, [B])
    turn.join(ctx, seats[A], "head-a")
    turn.join(ctx, seats[B], "head-b")
    for number in withdrawn:
        turn.withdraw(ctx, seats[number])

    assert_holder(ctx.store, seats, expected)
