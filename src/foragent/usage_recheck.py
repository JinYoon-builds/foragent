"""Re-read an exhausted executor's usage from a free source and end its pause early.

A usage limit pauses an executor until its announced reset (#105). That time is
only an upper bound: quotas sometimes reset early. While the pause lasts, the
gate re-reads usage every ``usage_recheck_minutes`` from a source that costs no
model turn:

* Codex: ``codex app-server`` answers ``account/rateLimits/read`` over JSON-RPC.
* Claude: the statusline wrapper's account snapshot
  ``$XDG_DATA_HOME/foragent/claude-rate-limits.json`` (see ``statusline``).

The pause clears only when every window is below 100 % and every window that
was exhausted reads below the level it had when the limit hit. A missing,
failing or stale source changes nothing, so the announced reset (or
``limit_wait_minutes``) still releases the work.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import TYPE_CHECKING, Any

from foragent.config import lane_executors
from foragent.orch_events import append_event
# Called through this module's globals, so tests and callers may replace them here.
from foragent.usage_sources import read_claude, read_codex

if TYPE_CHECKING:
    from foragent.scheduler import TickContext
    from foragent.state import StateStore

SOURCES = {"codex": "codex-app-server", "claude": "claude-statusline"}
_RECORD = "usage-recheck.json"


def _load(store: StateStore) -> dict[str, Any]:
    try:
        value = json.loads((store.root / _RECORD).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_pauses(store: StateStore) -> dict[str, str]:
    try:
        value = json.loads((store.root / "executor-pauses.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _pause_lane(store: StateStore, lane: str, until: datetime) -> None:
    """Hold ``lane`` until ``until``; the caller holds the state lock."""
    path = store.root / "lane-pauses.json"
    lanes = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if lane not in lanes or datetime.fromisoformat(lanes[lane]) < until:
        lanes[lane] = until.astimezone(UTC).isoformat()
    store._write_json(path, lanes)


def hold_lane(store: StateStore, executor: str, lane: str, now: datetime) -> datetime | None:
    """Pause ``lane`` behind an active pause of ``executor`` and return its end, or None when free.

    The check and the lane write share one state-lock section, so a recovery
    that clears the executor cannot leave a lane paused behind nothing.
    """
    with store._state_lock():
        stamp = _read_pauses(store).get(executor)
        until = datetime.fromisoformat(stamp) if stamp else None
        if until is None or now >= until:
            return None
        _pause_lane(store, lane, until)
        return until


def note_limit(store: StateStore, executor: str, until: datetime, windows: list[dict] | None, now: datetime,
               lane: str | None = None) -> datetime:
    """Pause ``executor`` (and ``lane``) and note which windows were exhausted, in one write.

    The pauses and the note change together under the state lock, so a recheck
    never sees one without the others. Like ``pause_executor`` the later deadline
    wins; an active pause's exhausted windows are kept and each window keeps its
    lowest (strictest) level, so a repeated limit never loosens recovery.
    Returns the deadline now in force.
    """
    exhausted = [{"name": window["name"], "used_percent": window.get("used_percent")}
                 for window in windows or [] if window.get("exhausted")]
    with store._state_lock():
        pauses = _read_pauses(store)
        active = datetime.fromisoformat(pauses[executor]) if executor in pauses else None
        active = active if active is not None and now < active else None
        records = _load(store)
        previous = records.get(executor)
        if (active is not None and isinstance(previous, dict) and previous.get("until")
                and datetime.fromisoformat(previous["until"]) == active):
            levels: dict[str, float | None] = {}
            for window in [*(previous.get("exhausted") or []), *exhausted]:
                name, level = window["name"], window.get("used_percent")
                if name not in levels or levels[name] is None:
                    levels[name] = level
                elif level is not None:
                    levels[name] = min(levels[name], level)
            exhausted = [{"name": name, "used_percent": level} for name, level in levels.items()]
        if active is not None and active > until:
            until = active
        stamp = until.astimezone(UTC).isoformat()
        pauses[executor] = stamp
        store._write_json(store.root / "executor-pauses.json", pauses)
        records[executor] = {"until": stamp, "since": now.astimezone(UTC).isoformat(),
                             "exhausted": exhausted, "checked_at": None}
        store._write_json(store.root / _RECORD, records)
        if lane is not None:
            _pause_lane(store, lane, until)
    return until


def _claim(store: StateStore, executor: str, pause: str, now: datetime, interval: timedelta) -> dict | None:
    """Mark a due recheck of the pause ``pause``, or return None when it is not due.

    Compare-and-set under the state lock: a pause or note written since ``pause``
    was read is newer than this tick's view, so it is left alone until next tick.
    """
    with store._state_lock():
        if _read_pauses(store).get(executor) != pause:
            return None
        records = _load(store)
        record = records.get(executor)
        until = datetime.fromisoformat(pause)
        if not isinstance(record, dict) or not record.get("until"):
            record = None
        elif datetime.fromisoformat(record["until"]) > until:
            return None  # A note ahead of its pause belongs to a writer that is not done.
        elif datetime.fromisoformat(record["until"]) < until:
            record = None  # The note of an older pause says nothing about this one.
        if record is None:
            record = {"until": until.astimezone(UTC).isoformat(), "since": None, "exhausted": [], "checked_at": None}
        checked = record.get("checked_at")
        if checked is not None and now - datetime.fromisoformat(checked) < interval:
            return None
        records[executor] = mark = {**record, "checked_at": now.astimezone(UTC).isoformat()}
        store._write_json(store.root / _RECORD, records)
        return mark


def recovered(windows: list[dict], exhausted: list[dict]) -> bool:
    """Every window has room and every exhausted window dropped below its level at the limit."""
    if any(window["used_percent"] >= 100 for window in windows):
        return False
    fresh = {window["name"]: window["used_percent"] for window in windows}
    for window in exhausted:
        level = window.get("used_percent")
        if level is None and window["name"] not in fresh:
            # A rejection that named no window (the parsed ``limit`` row) has no level
            # to compare; every window having room is the only evidence available.
            continue
        if window["name"] not in fresh or fresh[window["name"]] >= min(100, level if level is not None else 100):
            return False
    return True


def _clear(ctx: TickContext, executor: str, until: datetime, pause: str, mark: dict) -> bool:
    """End the executor pause and the pauses it put on lanes that use it.

    Only the pause that was read before the usage read ends: when another runner
    renewed or re-noted it meanwhile, the older reading says nothing about it.
    """
    store = ctx.store
    with store._state_lock():
        path = store.root / "executor-pauses.json"
        pauses = _read_pauses(store)
        if pauses.get(executor) != pause or _load(store).get(executor) != mark:
            return False
        pauses.pop(executor, None)
        store._write_json(path, pauses)
        records = _load(store)
        records.pop(executor, None)
        store._write_json(store.root / _RECORD, records)
        path = store.root / "lane-pauses.json"
        lanes = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        for lane, value in list(lanes.items()):
            if executor in lane_executors(ctx.config, lane) and datetime.fromisoformat(value) <= until:
                del lanes[lane]
        store._write_json(path, lanes)
    return True


def recheck(ctx: TickContext) -> list[str]:
    """Re-read usage of each paused executor that is due; return the executors that recovered."""
    now, interval = ctx.now(), timedelta(minutes=ctx.config.usage_recheck_minutes)
    pauses = _read_pauses(ctx.store)
    done = []
    for executor in sorted(pauses):
        until = ctx.store.executor_pause_until(executor, now)
        if executor not in SOURCES or until is None:
            continue
        # Marked before the read, so a renewal during the read is visible to ``_clear``.
        if (mark := _claim(ctx.store, executor, pauses[executor], now, interval)) is None:
            continue
        since = datetime.fromisoformat(mark["since"]) if mark.get("since") else None
        windows = read_codex(now) if executor == "codex" else read_claude(now, interval, since)
        if windows is None or not recovered(windows, mark.get("exhausted") or []):
            continue
        if not _clear(ctx, executor, until, pauses[executor], mark):
            continue
        levels = ", ".join(f"{window['name']} {window['used_percent']:g}%" for window in windows)
        append_event(ctx.store, now, None, "quota-recovered",
                     f"{executor} quota recovered before {until.isoformat()} per {SOURCES[executor]}: {levels}")
        done.append(executor)
    return done
