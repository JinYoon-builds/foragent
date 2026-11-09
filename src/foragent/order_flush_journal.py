"""Flush records batch membership and issue progress; Git owns commit/push facts.

Legacy Git checkpoint rows remain readable but play no part in recovery.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os

from foragent import order_notes
from foragent.file_locks import locked as lock_descriptor


JOURNAL = "flush-journal.jsonl"
_HELD = ContextVar("flush_journals", default=frozenset())


def entries(store) -> list[dict]:
    return order_notes.entries(store, JOURNAL)


def append(store, kind: str, batch: str, **values) -> None:
    with locked(store):
        _append(store, kind, batch, **values)


def _append(store, kind, batch, **values):
    payload = "\n" + json.dumps({"kind": kind, "batch": batch, **values}, ensure_ascii=False) + "\n"
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / JOURNAL).open("a", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def locked(store):
    key = str(store.root.resolve())
    held = _HELD.get()
    if key in held:
        yield
        return
    store.root.mkdir(parents=True, exist_ok=True)
    # Persistent inode: never unlink a lock that another flush may already have opened.
    fd = os.open(store.root / "flush.lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        with lock_descriptor(fd, store.root / "flush.lock", journal_root=store.root):
            token = _HELD.set(held | {key})
            try:
                yield
            finally:
                _HELD.reset(token)
    finally:
        os.close(fd)


def selected_entries(records: list[dict], *, done_only: bool = False) -> set[str]:
    done = {row["batch"] for row in records if row["kind"] == "done"}
    return {entry for row in records if row["kind"] == "selected"
            and (not done_only or row["batch"] in done)
            for entry in [*row["orders"], *row["notes"]]}


def selected_ids(records: list[dict]) -> dict[str, str]:
    """Order id of each selected order entry; the journal never keeps the order's plan."""
    return {entry: identity for row in records if row["kind"] in {"selected", "migrated"}
            for entry, identity in row.get("ids", {}).items()}


def published_entries(store) -> set[str]:
    records = entries(store)
    queued = [row for row in records if row["kind"] == "queued"]
    if queued:
        from foragent import order_publications

        requests = order_publications.requests(store)
        published = {row["id"] for row in requests if row["status"] == "published"}
        # A retained revision is consumed once the replacement that retired it lands.
        while replacements := {row["id"] for row in requests if row["status"] == "superseded"
                               and row.get("superseded_by") in published} - published:
            published.update(replacements)
        done = {row["batch"] for row in records if row["kind"] == "done"}
        handed_off = {row["batch"] for row in queued}
        records = [row for row in records if row["kind"] != "done" or row["batch"] not in handed_off]
        records += [{"kind": "done", "batch": row["batch"]} for row in queued
                    if set(row["publications"]) <= published
                    and (row.get("notes_batch") is None or row["notes_batch"] in done)]
    return selected_entries(records, done_only=True)
