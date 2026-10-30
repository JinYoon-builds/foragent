"""End-of-tick housekeeping: log retention on every tick, the tidy check on finished ticks."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from foragent import tidy
from foragent.orch_tick_outcome import step, timed


@contextmanager
def after_tick(ctx) -> Iterator[Callable]:
    """Wrap the body of a tick that took the lock; housekeeping runs on every exit.

    Yields ``done(report, finished=False) -> report`` for each return path.
    Journal trimming runs under the state lock and log deletion outside it
    (``StateStore.prune_logs``). Only a tick that finished normally checks
    tidy. On an error the logs are still pruned and the error propagates.
    """
    seen: dict = {"report": None, "finished": False}

    def done(report, finished: bool = False):
        seen.update(report=report, finished=finished)
        return report

    try:
        yield done
    finally:
        report = seen["report"]
        actions = report.actions if report is not None else []
        prune(ctx, actions)
        # After this tick's starts (idle lanes are really idle); a due tidy asks for judgment only.
        if seen["finished"] and (request := timed("tidy_check", tidy.post_if_due, ctx.store)):
            actions.append(f"Tidy due; inbox request {request['id']}")


def prune(ctx, actions: list[str]) -> None:
    """Trim old journal lines and prune logs; an OSError becomes one line and the tick continues."""
    now = ctx.now()
    with step("log_prune"):
        try:
            for path in ctx.store.trim_journals(now):
                actions.append(f"Trimmed {path.name} lines older than 30 days")
            ctx.store.prune_log_files(now)  # Journals were trimmed just above.
            if ctx.store.logs_total() > _limit():
                actions.append("Logs still above 500 MB after pruning; active runs' logs are kept")
        except OSError as exc:
            actions.append(f"Log pruning failed: {exc}")


def _limit() -> int:
    from foragent import state

    return state.LOG_LIMIT_BYTES
