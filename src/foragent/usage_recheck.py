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
import queue
import subprocess
import threading
from typing import TYPE_CHECKING, Any

from foragent import statusline
from foragent.config import resolved_reviewer
from foragent.orch_events import append_event
from foragent.usage_events import _number, _window_name

if TYPE_CHECKING:
    from foragent.scheduler import TickContext
    from foragent.state import StateStore

SOURCES = {"codex": "codex-app-server", "claude": "claude-statusline"}
RPC_TIMEOUT_S = 10.0
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


def note_limit(store: StateStore, executor: str, until: datetime, windows: list[dict] | None, now: datetime) -> None:
    """Pause ``executor`` and remember which windows were exhausted, and how full, in one write.

    The pause and its note change together under the state lock, so a recheck
    never sees one without the other. Like ``pause_executor`` the later deadline
    wins; an active pause's exhausted windows are kept and each window keeps its
    lowest (strictest) level, so a repeated limit never loosens recovery.
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


def read_codex(now: datetime) -> list[dict] | None:
    """Ask ``codex app-server`` for the account's rate limits; never starts a thread or turn."""
    try:
        process = subprocess.Popen(
            ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return None
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = datetime.now(UTC) + timedelta(seconds=RPC_TIMEOUT_S)

    def call(identity: int, method: str, params: dict | None = None) -> dict | None:
        process.stdin.write(json.dumps({"id": identity, "method": method, "params": params or {}}) + "\n")
        process.stdin.flush()
        while (remaining := (deadline - datetime.now(UTC)).total_seconds()) > 0:
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == identity:
                return message
        return None

    try:
        if call(1, "initialize", {"clientInfo": {"name": "foragent", "version": "0"}}) is None:
            return None
        process.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        process.stdin.flush()
        reply = call(2, "account/rateLimits/read")
    except (OSError, ValueError):
        return None
    finally:
        _close(process)
    result = reply.get("result") if isinstance(reply, dict) else None
    limits = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(limits, dict):
        return None
    windows = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict) or (percent := _number(window.get("usedPercent"))) is None:
            continue
        windows.append({"name": _window_name(window.get("windowDurationMins")) or key, "used_percent": percent})
    return windows or None


def _close(process: subprocess.Popen) -> None:
    try:
        process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def read_claude(now: datetime, interval: timedelta, since: datetime | None) -> list[dict] | None:
    """Read the statusline snapshot when it was observed recently, else nothing."""
    try:
        record = json.loads(statusline.usage_path().read_text(encoding="utf-8"))
        seen = datetime.fromisoformat(record["seen_at"])
        limits = record["rate_limits"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if seen.tzinfo is None or not isinstance(limits, dict):
        return None
    # The file's own observation time decides freshness, not its mtime.
    if now - seen > 2 * interval or seen > now or (since is not None and seen < since):
        return None
    windows = []
    for name, window in limits.items():
        if isinstance(window, dict) and (percent := _number(window.get("used_percentage"))) is not None:
            windows.append({"name": name, "used_percent": percent})
    return windows or None


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
            settings = ctx.config.lanes.get(lane, {}) if lane != "main" else {}
            implementer = settings.get("executor") or ctx.config.executor
            uses = executor in {implementer, resolved_reviewer(ctx.config)}
            if uses and datetime.fromisoformat(value) <= until:
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
