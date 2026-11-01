"""The last gate tick's outcome and the failure-streak alert (#387).

Every ``fa gate tick`` overwrites ``<state>/tick-outcome.json`` with a small
record: when it ran, ``ok`` or ``failed``, and the consecutive failure count. A
failed tick also records the exception type, a short message and the path of a
log that keeps the full traceback. When the streak reaches the repository's
``tick_failure_alert`` threshold, the gate puts one request in the orchestrator
inbox and sends one push. The next successful tick resets the streak and marks
that request done; it sends no push.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from foragent import i18n, notify, orch_inbox
from foragent.config import Config
from foragent.state import StateStore

OUTCOME_FILE = "tick-outcome.json"
LOG_FILE = "tick-failure.log"
MESSAGE_LIMIT = 500
SLOW_STEP_SECONDS = 60
T = TypeVar("T")

_PUSH = {
    "en": "Foragent gate ticks are failing",
    "ko": "Foragent 게이트 tick이 계속 실패해요",
}


def outcome_path(store: StateStore) -> Path:
    return store.root / OUTCOME_FILE


def read(store: StateStore) -> dict[str, Any] | None:
    """The last recorded tick outcome, or None when no readable record exists."""
    try:
        record = json.loads(outcome_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _summary(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= MESSAGE_LIMIT else text[:MESSAGE_LIMIT - 3] + "..."


class _Steps:
    """The ordered ``{name, seconds}`` timings of one recorded tick."""

    def __init__(self, language: str) -> None:
        self.language = language
        self.items: list[dict[str, Any]] = []


_CURRENT: ContextVar[_Steps | None] = ContextVar("tick_steps", default=None)


@contextmanager
def step(name: str) -> Iterator[None]:
    """Time one top-level tick step on the monotonic clock.

    Outside ``run_recorded`` this only runs the step. A step that raises still
    records its elapsed time. A step of 60 s or more writes one stderr line.
    Timing never changes what the step does.
    """
    steps = _CURRENT.get()
    if steps is None:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        seconds = time.monotonic() - started
        steps.items.append({"name": name, "seconds": seconds})
        if seconds >= SLOW_STEP_SECONDS:
            print(i18n.text("tick_step_slow", steps.language, step=name, seconds=seconds),
                  file=sys.stderr, flush=True)


def timed(name: str, call: Callable[..., T], *args: Any) -> T:
    """Run ``call(*args)`` as one timed tick step (see ``step``)."""
    with step(name):
        return call(*args)


def run_recorded(store: StateStore, config: Config, repo: str, run: Callable[[], T]) -> T | None:
    """Run one tick and record its outcome while holding the tick lock.

    Holding the lock across the record keeps outcomes in tick order. Returns
    None, recording nothing, when another tick holds the lock.
    """
    with store.hold_tick_lock() as acquired:
        if not acquired:
            return None
        steps = _Steps(config.language)
        token = _CURRENT.set(steps)
        started = time.monotonic()
        try:
            result = run()
        except Exception as exc:
            _CURRENT.reset(token)
            record_failure(store, config, repo, exc, steps=steps.items,
                           duration_seconds=_duration(started, steps.items))
            raise
        _CURRENT.reset(token)
        record_success(store, steps=steps.items, duration_seconds=_duration(started, steps.items))
        return result


def _duration(started: float, steps: list[dict[str, Any]]) -> float:
    # Rounding in separate differences must never make the steps exceed the whole.
    return max(time.monotonic() - started, sum(item["seconds"] for item in steps))


def record_success(store: StateStore, *, duration_seconds: float = 0,
                   steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Record a successful tick; close an open streak alert without a push.

    Callers hold the tick lock (see ``run_recorded``).
    """
    previous = read(store) or {}
    record = {"at": store._now().isoformat(), "status": "ok", "consecutive_failures": 0,
              "duration_seconds": duration_seconds, "steps": list(steps or [])}
    store._write_json(outcome_path(store), record)
    alert = previous.get("alert_request")
    if alert:
        try:
            recovered = f"gate tick recovered at {record['at']}"
            orch_inbox.set_request_status(store, alert, "done", recovered, answer=recovered)
        except orch_inbox.InboxError:
            pass  # The request was removed by hand; nothing is left to resolve.
    return record


def record_failure(store: StateStore, config: Config, repo: str, exc: BaseException, *,
                   duration_seconds: float = 0,
                   steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Record a failed tick with its traceback log; alert once when the streak reaches the threshold.

    Callers hold the tick lock (see ``run_recorded``).
    """
    previous = read(store) or {}
    streak = int(previous.get("consecutive_failures") or 0) + 1
    log_path = store.root / LOG_FILE
    at = store._now().isoformat()
    store._atomic_write(log_path, f"gate tick failed at {at}\n" + "".join(traceback.format_exception(exc)))
    record: dict[str, Any] = {
        "at": at, "status": "failed", "consecutive_failures": streak,
        "duration_seconds": duration_seconds, "steps": list(steps or []),
        "error_type": type(exc).__name__, "error_message": _summary(exc), "log_path": str(log_path),
    }
    alert = previous.get("alert_request")  # Success records never carry one.
    if alert:
        record["alert_request"] = alert
    # Write the streak before alerting so a failed alert never loses the count.
    store._write_json(outcome_path(store), record)
    if alert is None and streak >= config.tick_failure_alert:
        line = (f"{streak} consecutive gate ticks failed. Last error: "
                f"{record['error_type']}: {record['error_message']}. Traceback: {log_path}")
        request = orch_inbox.add_request(store, "gate", line, {"tick_failure_since": at})
        record["alert_request"] = request["id"]
        store._write_json(outcome_path(store), record)
        with notify.sending("blocked"):
            notify.send(replace(config, repo=repo), _PUSH.get(config.language, _PUSH["en"]), line)
    return record
