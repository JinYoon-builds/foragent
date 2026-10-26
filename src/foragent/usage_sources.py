"""Free usage sources for an exhausted executor; neither starts a model turn.

* Codex: ``codex app-server`` answers ``account/rateLimits/read`` over JSON-RPC.
* Claude: the statusline wrapper's account snapshot
  ``$XDG_DATA_HOME/foragent/claude-rate-limits.json`` (see ``statusline``).

Each reader returns ``[{"name", "used_percent"}]`` or None when the source is
missing, failing, malformed or stale.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import queue
import subprocess
import threading

from foragent import statusline
from foragent.usage_events import _number, _window_name

RPC_TIMEOUT_S = 10.0


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
