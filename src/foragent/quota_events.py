"""Start-time journal of the executor choice (#107, #186): switches and same-model reviews.

The tick is the one writer: ``start_run`` calls the journal under the lock that stores the run.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from foragent.config import Config, resolved_reviewer

if TYPE_CHECKING:
    from foragent.scheduler_quota import Switch
    from foragent.state import StateStore


def choose_executor(store: StateStore, config: Config, lane: str, now: datetime) -> tuple[str, str]:
    """The (executor, reviewer) pair a new start of ``lane`` would use now."""
    from foragent.scheduler_quota import decide

    start = decide(store, config, lane, now)
    return start.config.executor, resolved_reviewer(start.config)


def pair(start_config: Config) -> tuple[str, str | None]:
    """The concrete pair a run stores; a reviewer ``none`` is stored as None."""
    reviewer = resolved_reviewer(start_config)
    return start_config.executor, (None if reviewer == "none" else reviewer)


def switch_journal(store: StateStore, now: datetime, number: int, switch: Switch) -> Callable[[], None]:
    """Write the run's executor-switch event; ``start_run`` calls it under the lock that stores the run."""
    from foragent.orch_events import append_event

    return lambda: append_event(store, now, number, "executor-switch",
                                f"#{number} implementer {switch.source} -> {switch.target}: {switch.reason}",
                                locked=True, **{"from": switch.source, "to": switch.target, "reason": switch.reason})


def start_journal(store: StateStore, now: datetime, number: int, switch: Switch | None,
                  config: Config, lane: str, executor: str, reviewer: str | None) -> Callable[[], None] | None:
    """The switch event and, when the pair first becomes same-model, one same-model-review event.

    A reviewer the lane's own config names is ``configured``,
    otherwise the review moved for a ``limit``. A continued start with the same pair adds nothing.
    """
    from foragent.orch_events import append_event
    from foragent.run_implementation import lane_config
    from foragent.scheduler_quota import CONTINUED_OUTCOMES

    writes = [switch_journal(store, now, number, switch)] if switch is not None else []
    previous = store.get_run(number)
    before = (previous.executor, previous.reviewer) if previous is not None and \
        previous.outcome in CONTINUED_OUTCOMES else None
    if reviewer == executor and before != (executor, reviewer):
        reason = "configured" if resolved_reviewer(lane_config(config, lane)) == executor else "limit"
        writes.append(lambda: append_event(
            store, now, number, "same-model-review", f"#{number} {executor} reviews its own work: {reason}",
            locked=True, executor=executor, reason=reason))
    if not writes:
        return None
    return lambda: [write() for write in writes] and None


def switch_line(number: int, switch: Switch, language: str) -> str:
    """The tick report line for a switch, in the repository's language."""
    from foragent.i18n import text
    from foragent.scheduler_quota import label

    reason = text(switch.key, language, **{"name": label(switch.source), **switch.values})
    return text("executor_switch", language, number=number, source=label(switch.source),
                target=label(switch.target), reason=reason)
