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
import time
import traceback
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from foragent import notify, orch_inbox
from foragent.config import Config
from foragent.state import StateStore

OUTCOME_FILE = "tick-outcome.json"
LOG_FILE = "tick-failure.log"
MESSAGE_LIMIT = 500
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


def run_recorded(store: StateStore, config: Config, repo: str, run: Callable[[], T]) -> T | None:
    """Run one tick and record its outcome while holding the tick lock.

    Holding the lock across the record keeps outcomes in tick order. Returns
    None, recording nothing, when another tick holds the lock.
    """
    with store.hold_tick_lock() as acquired:
        if not acquired:
            return None
        started = time.monotonic()
        try:
            result = run()
        except Exception as exc:
            record_failure(store, config, repo, exc, duration_seconds=time.monotonic() - started)
            raise
        record_success(store, duration_seconds=time.monotonic() - started)
        return result


def record_success(store: StateStore, *, duration_seconds: float = 0) -> dict[str, Any]:
    """Record a successful tick; close an open streak alert without a push.

    Callers hold the tick lock (see ``run_recorded``).
    """
    previous = read(store) or {}
    record = {"at": store._now().isoformat(), "status": "ok", "consecutive_failures": 0,
              "duration_seconds": duration_seconds}
    store._write_json(outcome_path(store), record)
    alert = previous.get("alert_request")
    if alert:
        try:
            orch_inbox.set_request_status(store, alert, "done", f"gate tick recovered at {record['at']}")
        except orch_inbox.InboxError:
            pass  # The request was removed by hand; nothing is left to resolve.
    return record


def record_failure(store: StateStore, config: Config, repo: str, exc: BaseException, *,
                   duration_seconds: float = 0) -> dict[str, Any]:
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
        "duration_seconds": duration_seconds,
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
        notify.send(replace(config, repo=repo), _PUSH.get(config.language, _PUSH["en"]), line)
    return record
