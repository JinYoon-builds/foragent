"""Durable request and question box between planning sessions and the orchestrator (D76).

Each request and question is one JSON file under ``<state>/inbox``. Files survive
session restarts, so a new orchestrator reads the box before anything else.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from foragent import notify
from foragent.config import Config
from foragent.orch_events import append_event
from foragent.state import StateStore

REQUEST_STATUSES = ("new", "processing", "done")
# A planning session that renewed its heartbeat this recently can still read a
# cross-session message, so a new question does not need a push.
PLAN_HEARTBEAT_SECONDS = 120
_QUESTION_PUSH = {
    "en": ("A question needs your answer", "{id}: {question}"),
    "ko": ("답할 질문이 있어요", "{id}: {question}"),
}


class InboxError(ValueError):
    pass


def _folder(store: StateStore, kind: str) -> Path:
    return store.root / "inbox" / kind


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _records(store: StateStore, kind: str) -> list[dict[str, Any]]:
    folder = _folder(store, kind)
    if not folder.is_dir():
        return []
    rows = [_read(path) for path in folder.glob("*.json")]
    return sorted(rows, key=lambda row: int(row.get("seq", 0)))


def _create(store: StateStore, kind: str, prefix: str, fields: dict[str, Any]) -> dict[str, Any]:
    with store._state_lock():
        folder = _folder(store, kind)
        seq = max((int(row.get("seq", 0)) for row in _records(store, kind)), default=0) + 1
        record = {"id": f"{prefix}{seq}", "seq": seq, **fields,
                  "created_at": store._timestamp(), "updated_at": store._timestamp()}
        store._write_json(folder / f"{record['id']}.json", record)
    return record


def _update(store: StateStore, kind: str, record_id: str, change) -> dict[str, Any]:
    path = _folder(store, kind) / f"{record_id}.json"
    with store._state_lock():
        if "/" in record_id or record_id.startswith(".") or not path.is_file():
            raise InboxError(f"no {kind[:-1]} with id {record_id!r}")
        record = _read(path)
        change(record)
        record["updated_at"] = store._timestamp()
        store._write_json(path, record)
    return record


def add_request(store: StateStore, source: str, text: str) -> dict[str, Any]:
    if not text.strip():
        raise InboxError("request text must not be empty")
    record = _create(store, "requests", "r", {
        "from": source, "text": text, "status": "new", "notes": [],
    })
    append_event(store, store._now(), None, "request", f"{record['id']} from {source}: {text}")
    return record


def requests(store: StateStore, *, pending: bool = False) -> list[dict[str, Any]]:
    rows = [_public(row) for row in _records(store, "requests")]
    return [row for row in rows if row["status"] != "done"] if pending else rows


def set_request_status(store: StateStore, request_id: str, status: str, note: str | None = None) -> dict[str, Any]:
    if status not in REQUEST_STATUSES:
        raise InboxError(f"status must be one of: {', '.join(REQUEST_STATUSES)}")

    def change(record: dict[str, Any]) -> None:
        record["status"] = status
        record.setdefault("notes", []).append(
            {"at": store._timestamp(), "status": status, "note": note or ""})

    record = _update(store, "requests", request_id, change)
    append_event(store, store._now(), None, "request-status",
                 f"{request_id} {status}" + (f": {note}" if note else ""))
    return _public(record)


def ask(store: StateStore, question: str, options: list[str], recommend: int | None) -> dict[str, Any]:
    if not question.strip():
        raise InboxError("question must not be empty")
    if recommend is not None and not 1 <= recommend <= len(options):
        raise InboxError("--recommend must name one of the --option values (1-based)")
    record = _create(store, "questions", "q", {
        "question": question, "options": list(options), "recommend": recommend,
        "status": "open", "answer": None,
    })
    append_event(store, store._now(), None, "ask", f"{record['id']}: {question}")
    return _public(record)


def plan_heartbeat_path(store: StateStore) -> Path:
    return store.root / "plan-heartbeat.json"


def write_plan_heartbeat(store: StateStore) -> dict[str, str]:
    """Overwrite the planning session's heartbeat with the current time."""
    record = {"at": store._timestamp()}
    store.root.mkdir(parents=True, exist_ok=True)
    store._write_json(plan_heartbeat_path(store), record)
    return record


def planning_alive(store: StateStore) -> bool:
    """True when the planning heartbeat is at most PLAN_HEARTBEAT_SECONDS old."""
    try:
        at = datetime.fromisoformat(json.loads(plan_heartbeat_path(store).read_text(encoding="utf-8"))["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if at.tzinfo is None:
        return False
    return store._now() - at <= timedelta(seconds=PLAN_HEARTBEAT_SECONDS)


def route_question(store: StateStore, config: Config, repo: str, record: dict[str, Any]) -> bool:
    """Push a new question to the person only when no planning session is alive.

    Returns whether a push was attempted. A live planning session reads the box
    through a cross-session message instead.
    """
    if planning_alive(store):
        return False
    title, line = _QUESTION_PUSH.get(config.language, _QUESTION_PUSH["en"])
    notify.send(replace(config, repo=repo), title, line.format(**record))
    return True


def questions(store: StateStore, *, include_closed: bool = False) -> list[dict[str, Any]]:
    rows = [_public(row) for row in _records(store, "questions")]
    return rows if include_closed else [row for row in rows if row["status"] == "open"]


def answer(store: StateStore, question_id: str, text: str) -> dict[str, Any]:
    def change(record: dict[str, Any]) -> None:
        if record.get("status") != "open":
            raise InboxError(f"question {question_id} is already closed")
        record.update(status="closed", answer=text, answered_at=store._timestamp())

    record = _update(store, "questions", question_id, change)
    append_event(store, store._now(), None, "answer", f"{question_id}: {text}")
    return _public(record)


def _public(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "seq"}
