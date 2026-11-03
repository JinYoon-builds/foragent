"""Flush is the sole writer of this append-only publication checkpoint."""

from contextlib import contextmanager
import fcntl
import json
import os

from foragent import order_notes


JOURNAL = "flush-journal.jsonl"


def entries(store) -> list[dict]:
    return order_notes.entries(store, JOURNAL)


def append(store, kind: str, batch: str, **values) -> None:
    # Flush owns this file under locked(); planning's logs use their own single-write path.
    payload = "\n" + json.dumps({"kind": kind, "batch": batch, **values}, ensure_ascii=False) + "\n"
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / JOURNAL).open("a", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def locked(store):
    store.root.mkdir(parents=True, exist_ok=True)
    # Persistent inode: never unlink a lock that another flush may already have opened.
    fd = os.open(store.root / "flush.lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def selected_entries(records: list[dict], *, done_only: bool = False) -> set[str]:
    done = {row["batch"] for row in records if row["kind"] == "done"}
    return {entry for row in records if row["kind"] == "selected"
            and (not done_only or row["batch"] in done)
            for entry in [*row["orders"], *row["notes"]]}


def published_entries(store) -> set[str]:
    return selected_entries(entries(store), done_only=True)
