"""A role session that hit its Claude usage limit: one PM alarm, one wake at the reset (#2308).

Bug 2026-11-06: the orchestrator answered every event with "You've hit your 5-hour
limit · resets 12:10pm (Asia/Seoul)" for an hour, nobody was alarmed and nothing
woke it after the reset. This module is the one writer of
``<state>/usage-limit/<role>.json``, a record per role session:

- ``limited``: whether the role's latest reply is a usage-limit reply;
- ``since``, ``message``, ``reset_at`` (UTC, or None when the message names no
  time), ``reset_zone`` (the zone the message names, for display);
- ``alarmed``: the PM push for this episode was attempted (claimed before the push, never retried);
- ``woken_at``: the gate's one wake request after ``reset_at``;
- ``transcript`` and ``offset``: how far the orchestrator's transcript was read;
  ``seen_at``: the last role-session end read for another role.

Inputs: the orchestrator's active Claude transcript (``orch_sessions.active``), read
from the stored offset on every gate tick (``tick``), and the final message of a
gate-run role session (the ``<role>-done`` journal event ``orch_architect.run_session`` appends).
A usage-limit reply is the synthetic API-error message Claude Code writes
(``isApiErrorMessage`` with ``error: rate_limit``) or text naming the limit. A later
normal reply clears the episode, so the next limit alarms again.

The alarm is one ``notify`` push of kind ``orchestrator-stopped`` (the planning
session relays it; ``notify`` also leaves the journal notice). The wake is one inbox
request from the gate, which the orchestrator's ``fa orch watch --follow`` delivers
as a new event. ``active`` feeds ``fa brief --report``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from foragent import notify, orch_inbox, orch_sessions
from foragent.orch_events import append_event
from foragent.state import StateStore

ORCHESTRATOR = "orchestrator"
# Claude's limit replies: "You've hit your 5-hour limit · resets 12:10pm (Asia/Seoul)",
# "You've hit your usage limit", "Claude AI usage limit reached|1762398600".
LIMIT_TEXT = re.compile(r"hit your [\w -]*limit|usage limit reached|\b5-hour limit\b", re.I)
RESET_CLOCK = re.compile(
    r"resets\s+(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>am|pm)?"
    r"(?:\s*\((?P<zone>[A-Za-z_]+(?:/[A-Za-z_+-]+)*)\))?", re.I)
RESET_EPOCH = re.compile(r"limit reached\|(?P<epoch>\d{9,11})\b", re.I)
# The longest wait a reset may name; a later parse is not a reset time.
LONGEST_RESET = timedelta(days=8)

WORDS = {
    "en": {
        "title": "Usage limit",
        "alarm": "The {role} hit its Claude usage limit: \"{message}\". {when}",
        "when": "The gate wakes it at the reset, {reset}.",
        "no_reset": "The message names no reset time; the gate wakes it when it replies normally.",
        "report": "The {role} hit its usage limit; {when}",
        "report_when": "resets {reset}, the gate wakes it then.",
        "report_no_reset": "no reset time named.",
        "wake": "Wake: the {role}'s limit reset at {reset}. Check `fa gate status --json` and resume.",
        "wake_role": "The {role}'s limit reset at {reset}; call it again if its work is still needed.",
    },
    "ko": {
        "title": "사용 한도",
        "alarm": "{role}가 Claude 사용 한도에 걸렸습니다: \"{message}\". {when}",
        "when": "게이트가 리셋 시각 {reset}에 깨웁니다.",
        "no_reset": "메시지에 리셋 시각이 없습니다. 정상 응답이 오면 해제됩니다.",
        "report": "{role}가 사용 한도로 멈췄습니다. {when}",
        "report_when": "{reset}에 리셋되면 게이트가 깨웁니다.",
        "report_no_reset": "리셋 시각 없음.",
        "wake": "깨우기: {role}의 한도가 {reset}에 풀렸습니다. `fa gate status --json`을 보고 이어서 하세요.",
        "wake_role": "{role}의 한도가 {reset}에 풀렸습니다. 일이 남았으면 다시 부르세요.",
    },
}


def _words(language: str) -> dict[str, str]:
    return WORDS.get(language, WORDS["en"])


def _locked(store: StateStore):
    """This module's own lock (taken before, never inside, the state lock)."""
    from foragent.file_locks import file_lock

    store.root.mkdir(parents=True, exist_ok=True)
    return file_lock(store.root / "usage-limit.lock", journal_root=store.root)


def _folder(store: StateStore) -> Path:
    return store.root / "usage-limit"


def _path(store: StateStore, role: str) -> Path:
    return _folder(store) / f"{role}.json"


def read(store: StateStore, role: str) -> dict[str, Any]:
    try:
        record = json.loads(_path(store, role).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _write(store: StateStore, role: str, record: dict[str, Any]) -> None:
    _folder(store).mkdir(parents=True, exist_ok=True)
    store._write_json(_path(store, role), record)


def _at(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00") if isinstance(value, str) else value)
    except (TypeError, ValueError, AttributeError):
        return None
    return moment if moment.tzinfo is not None else None


def is_limit(text: str) -> bool:
    return bool(LIMIT_TEXT.search(text or ""))


def reset_time(text: str, sent: datetime) -> tuple[datetime | None, str | None]:
    """The reset the message names, as UTC, and the zone it names; (None, None) without one.

    A clock time ("resets 12:10pm (Asia/Seoul)") is its next occurrence after ``sent``
    in the named zone, else in UTC.
    """
    if match := RESET_EPOCH.search(text):
        return datetime.fromtimestamp(int(match["epoch"]), UTC), None
    match = RESET_CLOCK.search(text)
    if not match:
        return None, None
    hour, minute, half = int(match["hour"]), int(match["minute"] or 0), (match["half"] or "").lower()
    if half:
        if not 1 <= hour <= 12:
            return None, None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None, None
    zone_name = match["zone"]
    try:
        zone = ZoneInfo(zone_name) if zone_name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        zone, zone_name = UTC, None
    local = sent.astimezone(zone)
    reset = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= local:
        reset += timedelta(days=1)
    reset = reset.astimezone(UTC)
    return (reset, zone_name) if reset - sent <= LONGEST_RESET else (None, None)


def _show(at: datetime, zone_name: str | None) -> str:
    """``12:10 Asia/Seoul (03:10 UTC)``, or the UTC time alone."""
    utc = at.astimezone(UTC).strftime("%H:%M UTC")
    if not zone_name:
        return utc
    try:
        return f"{at.astimezone(ZoneInfo(zone_name)):%H:%M} {zone_name} ({utc})"
    except (ZoneInfoNotFoundError, ValueError):
        return utc


# --- the orchestrator's transcript -------------------------------------------------


def _reply(row: dict[str, Any]) -> tuple[str, str] | None:
    """("limit", text) or ("normal", text) for an assistant transcript row; None otherwise."""
    if row.get("type") != "assistant" or not isinstance(message := row.get("message"), dict):
        return None
    content = message.get("content")
    parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
    text = " ".join(part.get("text", "") for part in parts
                    if isinstance(part, dict) and isinstance(part.get("text"), str)).strip()
    if row.get("isApiErrorMessage"):
        return ("limit", text) if row.get("error") == "rate_limit" or is_limit(text) else None
    if message.get("model") == "<synthetic>":
        return ("limit", text) if is_limit(text) else None
    return "normal", text


def _latest_reply(path: Path, offset: int) -> tuple[tuple[str, str, datetime | None] | None, int]:
    """The last assistant reply after ``offset`` and the offset after the last whole line."""
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            if offset > size:
                offset = 0  # A rewritten transcript is read again.
            stream.seek(offset)
            data = stream.read()
    except OSError:
        return None, offset
    end = data.rfind(b"\n") + 1
    latest = None
    for line in data[:end].splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and (found := _reply(row)) is not None:
            latest = (*found, _at(row.get("timestamp")))
    return latest, offset + end


def _limited(record: dict[str, Any], message: str, sent: datetime) -> dict[str, Any]:
    """The record after one usage-limit reply: a new episode, or a later reset for the current one."""
    reset, zone = reset_time(message, sent)
    if not record.get("limited"):
        return {**record, "limited": True, "since": sent.isoformat(), "message": message[:300],
                "reset_at": reset.isoformat() if reset else None, "reset_zone": zone,
                "alarmed": False, "woken_at": None}
    current = _at(record.get("reset_at"))
    if reset is not None and (current is None or reset > current) and record.get("woken_at"):
        # Still limited after the wake: the newer reset is the next wake time.
        return {**record, "message": message[:300], "reset_at": reset.isoformat(), "reset_zone": zone,
                "woken_at": None}
    if reset is not None and current is None:
        return {**record, "message": message[:300], "reset_at": reset.isoformat(), "reset_zone": zone}
    return record


def _cleared(record: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The record after a normal reply; ``_store`` appends the clear event once it is written."""
    kept = {key: value for key, value in record.items()
            if key in ("transcript", "offset", "seen_at", "seen_started")}
    return kept | {"limited": False, "cleared_at": now.isoformat()}


def _store(store: StateStore, role: str, before: dict[str, Any], after: dict[str, Any], now: datetime) -> None:
    """Write the record first, then the journal fact: a failed write repeats no event."""
    _write(store, role, after)
    if before.get("limited") and not after.get("limited"):
        append_event(store, now, None, "usage-limit-clear", f"{role} replies normally again", role=role)


# --- the episode's alarm and wake ----------------------------------------------------
# Every effect that reaches the PM is claimed or found under ``_locked`` before it is made, so a
# failed delivery, a failed record write or a crash never repeats it on the next tick (#2308).


def _claim_alarm(store: StateStore, role: str, now: datetime) -> dict[str, Any] | None:
    """Mark this episode alarmed before the push; None when it needs no alarm."""
    with _locked(store):
        record = read(store, role)
        if not record.get("limited") or record.get("alarmed"):
            return None
        record = {**record, "alarmed": True, "alarmed_at": now.isoformat()}
        _write(store, role, record)
    return record


def _alarm(store: StateStore, config, root: Path, role: str, now: datetime) -> None:
    """One push attempt per episode.

    A failed push is not retried: ``notify.deliver`` already keeps the failure as a
    notice the PM reads, and ``fa brief --report`` names the limit while it lasts.
    """
    record = _claim_alarm(store, role, now)
    if record is None:
        return
    words = _words(config.language)
    reset = _at(record.get("reset_at"))
    when = words["when"].format(reset=_show(reset, record.get("reset_zone"))) if reset else words["no_reset"]
    line = words["alarm"].format(role=role, message=record.get("message") or "", when=when)
    error = None
    try:
        with notify.sending("orchestrator-stopped" if role == ORCHESTRATOR else "status", root) as sending:
            if notify.send(config, f"{words['title']}: {root.name}", line) is not True:
                error = sending.error or "not delivered"
    except Exception as exc:  # noqa: BLE001 - recorded; the report still names the limit
        error = " ".join(f"{type(exc).__name__}: {exc}".split())[:200]
    append_event(store, now, None, "usage-limit", line, role=role, reset_at=record.get("reset_at"),
                 **({"error": error} if error else {}))


def _wake_request(store: StateStore, role: str, record: dict[str, Any]) -> dict[str, Any] | None:
    """This episode's wake request for this reset, if the inbox already holds it."""
    for row in orch_inbox.requests(store):
        if (row.get("wake") == "usage-limit" and row.get("limit_role") == role
                and row.get("limit_since") == record.get("since") and row.get("limit_reset") == record.get("reset_at")):
            return row
    return None


def _wake(store: StateStore, config, role: str, now: datetime) -> None:
    """One gate request at or after the named reset; its new id is the watch's next event.

    The inbox itself says whether this episode's wake for this reset exists, so a
    record write that fails after the request never adds a second one.
    """
    with _locked(store):
        record = read(store, role)
        reset = _at(record.get("reset_at"))
        if not record.get("limited") or reset is None or now < reset or record.get("woken_at"):
            return
        request = _wake_request(store, role, record)
        if request is None:
            words = _words(config.language)
            shown = _show(reset, record.get("reset_zone"))
            line = (words["wake"] if role == ORCHESTRATOR else words["wake_role"]).format(role=role, reset=shown)
            request = orch_inbox.add_request(store, "gate", line, {
                "wake": "usage-limit", "limit_role": role, "limit_since": record.get("since"),
                "limit_reset": record.get("reset_at")})
        _write(store, role, {**record, "woken_at": now.isoformat(), "wake_request": request["id"]})


def tick(ctx) -> None:
    """Every gate tick: read the orchestrator's new replies, then alarm or wake every limited role."""
    store, config, now = ctx.store, ctx.config, ctx.now()
    root = Path(ctx.repo_root).resolve()
    with _locked(store):
        before = read(store, ORCHESTRATOR)
        session = orch_sessions.active(store)
        if session is not None:
            path = session["transcript_path"]
            offset = before.get("offset", 0) if before.get("transcript") == path else 0
            latest, offset = _latest_reply(Path(path), offset if isinstance(offset, int) else 0)
            record = {**before, "transcript": path, "offset": offset}
            if latest is not None:
                kind, message, sent = latest
                record = (_limited(record, message, sent or now) if kind == "limit"
                          else _cleared(record, now))
            _store(store, ORCHESTRATOR, before, record, now)
    _observe_roles(store, now)
    for path in sorted(_folder(store).glob("*.json")) if _folder(store).is_dir() else []:
        _alarm(store, config, root, path.stem, now)
        _wake(store, config, path.stem, now)


def on_tick(ctx, report) -> None:
    """``tick`` for the gate; a failure is one report line and never stops the tick."""
    try:
        tick(ctx)
    except Exception as exc:  # noqa: BLE001 - the optional alarm must not stop the tick
        report.actions.append("usage-limit check failed: " + " ".join(f"{type(exc).__name__}: {exc}".split())[:200])


def _role_ends(store: StateStore) -> list[tuple[str, datetime, str, str]]:
    """Gate-run role sessions' ends: ``<role>-done`` events (``orch_architect.run_session``) with their message."""
    from foragent.file_cache import read_jsonl

    ends = []
    for event in read_jsonl(store.root / "orch-events.jsonl", tolerant=True, errors="replace"):
        kind = event.get("kind")
        if (isinstance(kind, str) and kind.endswith("-done") and "started_at" in event and "exit_code" in event
                and (at := _at(event.get("at"))) is not None and isinstance(event.get("message"), str)):
            ends.append((kind[:-len("-done")], at, str(event["started_at"]), event["message"]))
    return ends


def _observe_roles(store: StateStore, now: datetime) -> None:
    """A role session's final message after the last one read starts, extends or clears its episode."""
    ends = _role_ends(store)
    with _locked(store):
        for role in sorted({end[0] for end in ends if end[0] != ORCHESTRATOR}):
            record = read(store, role)
            # A session end is ordered by (at, started_at); ends at the same moment differ by their session.
            seen = _at(record.get("seen_at"))
            last = (seen, str(record.get("seen_started") or ""))
            new = [(at, started, message) for name, at, started, message in ends
                   if name == role and (seen is None or (at, started) > last)]
            if not new:
                continue
            at, started, message = max(new, key=lambda end: (end[0], end[1]))
            mark = {"seen_at": at.isoformat(), "seen_started": started}
            if seen is None and not is_limit(message):
                _write(store, role, {"limited": False, **mark})
                continue
            after = _limited(record, message, at) if is_limit(message) else _cleared(record, now)
            _store(store, role, record, {**after, **mark}, now)


def active(store: StateStore) -> list[dict[str, Any]]:
    """Every role stopped by its usage limit now, for the report."""
    rows = []
    for path in sorted(_folder(store).glob("*.json")) if _folder(store).is_dir() else []:
        record = read(store, path.stem)
        if record.get("limited"):
            rows.append({"role": path.stem, "since": record.get("since"), "message": record.get("message"),
                         "reset_at": record.get("reset_at"), "reset_zone": record.get("reset_zone")})
    return rows


def report_line(rows: list[dict[str, Any]], language: str) -> str | None:
    """The report's line for the first limited role, or None."""
    if not rows:
        return None
    row, words = rows[0], _words(language)
    reset = _at(row.get("reset_at"))
    when = (words["report_when"].format(reset=_show(reset, row.get("reset_zone"))) if reset
            else words["report_no_reset"])
    return words["report"].format(role=row["role"], when=when)
