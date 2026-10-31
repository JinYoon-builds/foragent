"""Durable request and question box between planning sessions and the orchestrator (D76).

Each request and question is one JSON file under ``<state>/inbox``. Files survive
session restarts, so a new orchestrator reads the box before anything else.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from foragent import notify
from foragent.config import Config
from foragent.orch_events import append_event
from foragent.state import StateStore

REQUEST_STATUSES = ("new", "processing", "done")
# What a person's question decides (#970): a D71 question, a merge approval or an order card.
QUESTION_KINDS = ("question", "merge", "order")
# A planning session that renewed its heartbeat this recently can still read a
# cross-session message, so a new question does not need a push.
PLAN_HEARTBEAT_SECONDS = 120
# The statusline renews an existing planning heartbeat only once it is this old.
PLAN_RENEW_SECONDS = 60
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


def clean(text: str) -> str:
    """``text`` with anything UTF-8 cannot encode (a lone surrogate such as \\ud800) replaced by "?".

    Box strings are outside input: a JSON escape or an undecodable file name can carry
    a lone surrogate, which would fail when the line is written out.
    """
    return text.encode("utf-8", "replace").decode("utf-8")


def _name(kind: str, path: Path) -> str:
    """A box file's name as one JSON string: a file name is outside input too."""
    return json.dumps(clean(f"inbox/{kind}/{path.name}"), ensure_ascii=False)


def _text(value: Any) -> bool:
    return isinstance(value, str)


def _optional_text(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _whole(value: Any) -> bool:
    return type(value) is int  # bool is not a count or a number here


def _optional_whole(value: Any) -> bool:
    return value is None or type(value) is int


def _texts(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _wholes(value: Any) -> bool:
    return isinstance(value, list) and all(type(item) is int for item in value)


def _items(value: Any) -> bool:
    return isinstance(value, list)


# The fields each reader relies on: (name, check, required). A field that is absent
# but optional is fine (older rows); a field that is present must have its type.
# Request notes are a list; a single malformed note inside is ignored on its own
# by the age rule, as before. Times are checked where the age is computed.
_ROW_FIELDS = {
    "requests": (("id", _text, True), ("seq", _whole, True), ("status", _text, True),
                 ("from", _text, True), ("text", _text, True), ("notes", _items, False)),
    "questions": (("id", _text, True), ("seq", _whole, True), ("status", _text, True),
                  ("question", _text, True), ("options", _texts, True),
                  ("recommend", _optional_whole, False), ("kind", _text, False),
                  ("issues", _wholes, False)),
}
# Every journal event, whatever its kind (append_event and the phase journal write
# these): at (checked as a time), kind, summary, issue and the optional say line.
JOURNAL_FIELDS = (("kind", _text, True), ("summary", _text, True), ("issue", _optional_whole, False),
                  ("say", _text, False))
# What a notice adds on top.
NOTICE_FIELDS = (("status", _text, True), ("channel", _optional_text, False), ("ref", _optional_text, False))


def _wrong_fields(row: dict[str, Any], fields) -> list[str]:
    """Names of fields that are missing though required, or present with the wrong type."""
    return [name for name, check, required in fields
            if (name in row and not check(row[name])) or (required and name not in row)]


def scan(store: StateStore, kind: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Every well-formed row of one box, oldest first, and a note for each file left out.

    A reader for the planning session's context and statusline: box files are outside
    input, so one corrupt file is skipped alone instead of hiding the others, and the
    notes let the caller tell a partial read from a complete empty box. No lock is
    taken; writers replace whole files, so a reader never sees half a row.
    """
    folder = _folder(store, kind)
    # os.scandir, not Path.glob: glob turns an unreadable folder into an empty one.
    # Only a missing folder is an empty box; a folder that cannot be listed, or a
    # file in its place, is a failed read.
    try:
        with os.scandir(folder) as entries:
            names = sorted(entry.name for entry in entries if entry.name.endswith(".json"))
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        where = json.dumps(f"inbox/{kind}", ensure_ascii=False)
        return [], [f"{where}: unreadable ({type(exc).__name__}: {exc.strerror or exc})"]
    paths = [folder / name for name in names]
    rows, skipped = [], []
    for path in paths:
        try:
            row = _read(path)
        except Exception as exc:  # any failure (bad JSON, deep nesting's RecursionError) skips this file alone
            skipped.append(f"{_name(kind, path)}: unreadable ({type(exc).__name__})")
            continue
        if not isinstance(row, dict):
            skipped.append(f"{_name(kind, path)}: not a {kind[:-1]} record (not an object)")
            continue
        wrong = _wrong_fields(row, _ROW_FIELDS[kind])
        if wrong:
            skipped.append(f"{_name(kind, path)}: not a {kind[:-1]} record (wrong {', '.join(wrong)})")
            continue
        rows.append(row)
    rows.sort(key=lambda row: row["seq"])
    return [_public(row) for row in rows], skipped


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


def add_request(store: StateStore, source: str, text: str,
                extra: dict[str, Any] | None = None) -> dict[str, Any]:
    if not text.strip():
        raise InboxError("request text must not be empty")
    record = _create(store, "requests", "r", {
        **(extra or {}), "from": source, "text": text, "status": "new", "notes": [],
    })
    append_event(store, store._now(), None, "request", f"{record['id']} from {source}: {text}")
    return record


def requests(store: StateStore, *, pending: bool = False) -> list[dict[str, Any]]:
    rows = [_public(row) for row in _records(store, "requests")]
    return [row for row in rows if row["status"] != "done"] if pending else rows


def set_request_status(store: StateStore, request_id: str, status: str, note: str | None = None,
                       check=None, *, answer: str | None = None, require_answer: bool = False,
                       empty_answer_reason: str | None = None) -> dict[str, Any]:
    """Set status and optional answer atomically; ``check(record)`` runs under the same lock.

    CLI completion requires an answer or explicit empty reason; signals use their own check hook.
    """
    if status not in REQUEST_STATUSES:
        raise InboxError(f"status must be one of: {', '.join(REQUEST_STATUSES)}")

    has_answer = bool(answer and answer.strip())
    has_reason = bool(empty_answer_reason and empty_answer_reason.strip())
    if status == "done" and require_answer and not (has_answer or has_reason):
        raise InboxError("closing a request needs a non-empty --answer")

    def change(record: dict[str, Any]) -> None:
        if require_answer and (status == "done" or answer is not None):
            from foragent.orch_role_raise import is_signal_request

            if is_signal_request(record):
                raise InboxError("close role signals with fa orch signal-answer")
        if check is not None:
            check(record)
        if answer is not None:
            record["answer"] = answer
            record.pop("empty_answer_reason", None)
        elif status == "done" and has_reason:
            record.pop("answer", None)
            record["empty_answer_reason"] = empty_answer_reason
        record["status"] = status
        record.setdefault("notes", []).append(
            {"at": store._timestamp(), "status": status, "note": note or ""})

    record = _update(store, "requests", request_id, change)
    append_event(store, store._now(), None, "request-status",
                 f"{request_id} {status}" + (f": {note}" if note else ""))
    return _public(record)


def annotate_request(store: StateStore, request_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Set ``fields`` on one request under the state lock; its status and notes stay as they are."""
    return _public(_update(store, "requests", request_id, lambda record: record.update(fields)))


def ask(store: StateStore, question: str, options: list[str], recommend: int | None,
        issues: list[int] = (), kind: str = "question") -> dict[str, Any]:
    """Record an open question; ``issues`` are the issues that wait on its answer."""
    if kind not in QUESTION_KINDS:
        raise InboxError(f"--kind must be one of: {', '.join(QUESTION_KINDS)}")
    if not question.strip():
        raise InboxError("question must not be empty")
    if recommend is not None and not 1 <= recommend <= len(options):
        raise InboxError("--recommend must name one of the --option values (1-based)")
    if any(number < 1 for number in issues):
        raise InboxError("--issue must be a positive issue number")
    record = _create(store, "questions", "q", {
        "kind": kind, "question": question, "options": list(options), "recommend": recommend,
        "status": "open", "answer": None,
        "issues": list(dict.fromkeys(issues)),
    })
    append_event(store, store._now(), None, "ask", f"{record['id']}: {question}")
    return _public(record)


def ask_once(store: StateStore, key: str, question: str, options: list[str], recommend: int) -> dict[str, Any] | None:
    """Record an open question unless one with this ``key`` was ever recorded; None when it was."""
    with store._state_lock():
        if any(row.get("key") == key for row in _records(store, "questions")):
            return None
        seq = max((int(row.get("seq", 0)) for row in _records(store, "questions")), default=0) + 1
        record = {"id": f"q{seq}", "seq": seq, "key": key, "kind": "question",
                  "question": question, "options": list(options),
                  "recommend": recommend, "status": "open", "answer": None, "issues": [],
                  "created_at": store._timestamp(), "updated_at": store._timestamp()}
        store._write_json(_folder(store, "questions") / f"{record['id']}.json", record)
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


def _plan_heartbeat_age(store: StateStore) -> timedelta | None:
    try:
        at = datetime.fromisoformat(json.loads(plan_heartbeat_path(store).read_text(encoding="utf-8"))["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return store._now() - at if at.tzinfo is not None else None


def planning_alive(store: StateStore) -> bool:
    """True when the planning heartbeat is at most PLAN_HEARTBEAT_SECONDS old."""
    age = _plan_heartbeat_age(store)
    return age is not None and age <= timedelta(seconds=PLAN_HEARTBEAT_SECONDS)


def renew_plan_heartbeat(store: StateStore) -> bool:
    """Refresh an existing planning heartbeat older than PLAN_RENEW_SECONDS; never create one.

    The planning session's own ``fa plan heartbeat`` creates the file; the
    statusline of that session keeps it fresh (#970).
    """
    if not plan_heartbeat_path(store).is_file():
        return False
    age = _plan_heartbeat_age(store)
    if age is not None and age <= timedelta(seconds=PLAN_RENEW_SECONDS):
        return False
    write_plan_heartbeat(store)
    return True


def route_question(store: StateStore, config: Config, repo: str, record: dict[str, Any]) -> bool:
    """Push a new question to the person only when no planning session is alive.

    Returns whether a push was attempted. A live planning session reads the box
    through a cross-session message instead.
    """
    if planning_alive(store):
        return False
    title, line = _QUESTION_PUSH.get(config.language, _QUESTION_PUSH["en"])
    with notify.sending("question"):
        notify.send(replace(config, repo=repo), title, line.format(**record))
    return True


def questions(store: StateStore, *, include_closed: bool = False) -> list[dict[str, Any]]:
    rows = [_public(row) for row in _records(store, "questions")]
    return rows if include_closed else [row for row in rows if row["status"] == "open"]


def waiting_issues(store: StateStore) -> set[int]:
    """Issue numbers linked to an open question; the board shows them in Your turn."""
    return {number for row in questions(store) for number in row.get("issues", [])}


def answer(store: StateStore, question_id: str, text: str) -> dict[str, Any]:
    def change(record: dict[str, Any]) -> None:
        if record.get("status") != "open":
            raise InboxError(f"question {question_id} is already closed")
        record.update(status="closed", answer=text, answered_at=store._timestamp())

    record = _update(store, "questions", question_id, change)
    append_event(store, store._now(), None, "answer", f"{question_id}: {text}")
    return _public(record)


# A notice is one journal event about a notification's delivery (#1026; #1030 writes them):
# {"at", "issue", "kind": "notice", "summary", "status", "channel", "ref"}.
NOTICE_KIND = "notice"
# The planning context reads only the journal's tail, so a long journal cannot delay a prompt.
NOTICE_TAIL_BYTES = 256 * 1024


def record_notice(store: StateStore, status: str, summary: str, *, channel: str | None = None,
                  ref: str | None = None, issue: int | None = None) -> dict[str, Any]:
    """Append one notification status fact to the journal.

    ``status`` is what happened (for example "sent" or "failed"), ``channel`` where it
    went, ``ref`` the box record it was about (a question or request id).
    """
    if not status.strip() or not summary.strip():
        raise InboxError("a notice needs a status and a summary")
    return append_event(store, store._now(), issue, NOTICE_KIND, summary,
                        status=status, channel=channel, ref=ref)


def recent_notices(store: StateStore, since: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """Notices recorded after ``since``, oldest first, and one note for each line left out.

    JSONL framing is "\\n" only: a field may hold U+2028 or another character that
    str.splitlines would split on. Every line is parsed, and notices are chosen and
    ordered on parsed fields only, never on the raw text (a key may be escaped).
    """
    path = store.root / "orch-events.jsonl"
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(0, size - NOTICE_TAIL_BYTES)
            # One byte more: whether the tail begins right after a newline.
            stream.seek(max(0, start - 1))
            data = stream.read()
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f"orch-events.jsonl: unreadable ({exc.strerror or exc})"]
    lines = data.split(b"\n")
    if start > 0:
        # The first piece is the byte before the tail plus whatever of a record the
        # tail cut into; when that byte is a newline the piece is empty.
        lines = lines[1:]
    events, problems = [], []
    for number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:  # any failure (bad JSON, deep nesting's RecursionError) skips this line alone
            problems.append(f"orch-events.jsonl: tail line {number} unreadable ({type(exc).__name__})")
            continue
        if not isinstance(event, dict):
            problems.append(f"orch-events.jsonl: tail line {number} is not an event (not an object)")
            continue
        at = _event_at(event)
        wrong = (["at"] if at is None else []) + _wrong_fields(event, JOURNAL_FIELDS)
        if event.get("kind") == NOTICE_KIND:
            wrong += _wrong_fields(event, NOTICE_FIELDS)
        if wrong:
            what = "notice" if event.get("kind") == NOTICE_KIND else "event"
            problems.append(f"orch-events.jsonl: tail line {number} is not a readable {what} "
                            f"(wrong {', '.join(wrong)})")
            continue
        events.append((number, at, event))
    if start > 0:
        # Only the tail was read. Unless it reaches back past ``since``, notices in the
        # window may sit in the part not read: say so instead of looking complete.
        earliest = events[0][1] if events else None
        if earliest is None or earliest > since:
            problems.insert(0, f"orch-events.jsonl: only the last {NOTICE_TAIL_BYTES} bytes read; "
                               "earlier notices in the window may be missing")
    notices = []
    for _number, at, event in events:
        if event["kind"] == NOTICE_KIND and at > since:
            notices.append((at, event))
    notices.sort(key=lambda item: item[0])
    return [event for _, event in notices], problems


def _event_at(event: dict[str, Any]) -> datetime | None:
    """A journal event's parsed timezone-aware time, else None."""
    try:
        at = datetime.fromisoformat(event["at"])
    except Exception:
        return None
    return at if at.tzinfo is not None else None


class FollowEvents:
    """New request ids and newly answered questions since watch started (#970).

    The baseline is read when the watch starts, not at its first successful
    poll, so records written while early polls fail still emit. If even that
    read fails, only records stamped before the start count as seen. Records
    present at the start, including ones from an earlier run, count as seen.
    Status notes on a request are not new events.
    """

    def __init__(self, store: StateStore) -> None:
        self.started = store._now()
        self.requests: set[str] | None = None
        self.answers: set[str] = set()
        try:
            self.requests, self.answers = self._ids(*self._read(store))
        except Exception:
            self.requests = None  # decided from timestamps at the first good poll

    @staticmethod
    def _read(store: StateStore) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows = _records(store, "requests")
        answered = [row for row in _records(store, "questions") if row.get("status") == "closed"]
        return rows, answered

    @staticmethod
    def _ids(rows, answered) -> tuple[set[str], set[str]]:
        return {row["id"] for row in rows}, {row["id"] for row in answered}

    def _before_start(self, value: Any) -> bool:
        try:
            return datetime.fromisoformat(value) < self.started
        except (TypeError, ValueError):
            return False  # an unreadable time emits rather than hides the record

    def poll(self, store: StateStore) -> list[dict[str, Any]]:
        rows, answered = self._read(store)
        if self.requests is None:
            self.requests = {row["id"] for row in rows if self._before_start(row.get("created_at"))}
            self.answers = {row["id"] for row in answered if self._before_start(row.get("answered_at"))}
        events = [{"event": "request", "id": row["id"], "from": row.get("from"), "text": row.get("text")}
                  for row in rows if row["id"] not in self.requests]
        events += [{"event": "answer", "id": row["id"], "kind": row.get("kind", "question"),
                    "answer": row.get("answer")} for row in answered if row["id"] not in self.answers]
        requests, answers = self._ids(rows, answered)
        self.requests |= requests
        self.answers |= answers
        return events


def _public(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "seq"}


def note_path(store: StateStore) -> Path:
    return store.root / store.ORCH_NOTE_FILE


def note_location(environ: dict[str, str], title: str | None = None,
                  tmux_pane: str | None = None) -> dict[str, Any]:
    """Where this orchestrator session runs, from facts in its environment only."""
    handle = environ.get("ORCA_TERMINAL_HANDLE")
    # An explicit Orca title is a fact too, even when the handle is not in this environment.
    if handle or (title and not tmux_pane):
        return {"kind": "orca", "handle": handle or None,
                "title": title or environ.get("FORAGENT_ORCH_TITLE")}
    pane = tmux_pane or (environ.get("TMUX_PANE") if environ.get("TMUX") else None)
    if pane:
        return {"kind": "tmux", "pane": pane}
    return {"kind": "terminal"}


def write_note(store: StateStore, now: str, why: str | None, next_step: str | None,
               where: dict[str, Any]) -> dict[str, Any]:
    """Overwrite the orchestrator's latest note: what it does, why, what next, where."""
    if not now.strip():
        raise InboxError("--now must not be empty")
    record = {"now": now, "why": why, "next": next_step, "at": store._timestamp(), "where": where}
    store.root.mkdir(parents=True, exist_ok=True)
    store._write_json(note_path(store), record)
    return record


def person_found(store: StateStore, text: str) -> dict[str, Any]:
    """Append one ``person-found`` event: a problem the person found before the retrospective."""
    if not text.strip():
        raise InboxError("--person-found must not be empty")
    return append_event(store, store._now(), None, "person-found", text)


def read_note(store: StateStore) -> dict[str, Any] | None:
    return store.orch_note()
