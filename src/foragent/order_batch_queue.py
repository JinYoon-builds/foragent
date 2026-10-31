"""The local order-batch queue file: waiting rows and the one pending batch (D85, #1043).

Everything lives in the repository's ignored ``.foragent/order-batch.json``. ``batch`` is a
single field, never a list: at most one batch is committed and not yet published.
"""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from foragent import order, order_references
from foragent.i18n import text
from foragent.order_files import write_atomic
from foragent.state_locks import _file_lock

QUEUE_FILE = "order-batch.json"
WAITING, COMMITTED, PUBLISHED = "waiting", "committed", "published"


def _directory(root: Path) -> Path:
    """The ignored local control directory; a planted symlink must not redirect it."""
    directory = order_references.real_directory(root, ".foragent")
    ignore = directory / ".gitignore"
    if ignore.is_symlink():
        raise order.PlanError(f"{ignore} must be a regular file, not a symlink")
    if not ignore.exists() or ignore.read_bytes() == b"":
        write_atomic(root, ".foragent/.gitignore", b"*\n")  # never a half-written ignore rule
    return directory


@contextmanager
def _lock(root: Path, name: str):
    path = _directory(root) / name
    if path.is_symlink():
        raise order.PlanError(f"{path} must be a regular file, not a symlink")
    with _file_lock(path):
        yield


def _load(root: Path) -> dict:
    path = root / ".foragent" / QUEUE_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if not path.is_symlink() else {}
    except FileNotFoundError:
        value = {}
    except ValueError as exc:
        raise order.PlanError(f"{path} is not valid JSON") from exc
    value = value if isinstance(value, dict) else {}
    batch = value.get("batch")
    return {"orders": list(value.get("orders", [])), "notes": list(value.get("notes", [])),
            "batch": batch if isinstance(batch, dict) else None}


def _update(root: Path, change) -> dict:
    with _lock(root, "order-batch-state.lock"):
        state = _load(root)
        change(state)
        directory = _directory(root)
        # A fresh exclusive temporary name, then an atomic rename over the queue file.
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=".order-batch.",
                                         suffix=".tmp", delete=False) as stream:
            stream.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stream.name, directory / QUEUE_FILE)
        return state


def _mark(root: Path, kind: str, revisions: set[str], **fields) -> None:
    def change(current: dict) -> None:
        for row in current[kind]:
            if row.get("revision", row["id"]) in revisions and row["status"] != PUBLISHED:
                row.update(fields)

    _update(root, change)


def _set_batch(root: Path, **fields) -> None:
    """Update (or with ``batch=None`` drop) the one pending batch record."""
    def change(current: dict) -> None:
        if "batch" in fields:
            current["batch"] = fields["batch"]
        elif current["batch"] is not None:
            current["batch"].update(fields)

    _update(root, change)


def _claim(root: Path, batch: dict, language: str) -> None:
    """Save a new batch only if every member is still the same waiting row (one queue update).

    The members were read before the batch was prepared; a re-defer or another flush in that
    window replaced or took one, so nothing is saved and the flush stops before any write.
    """
    def change(current: dict) -> None:
        orders = {row.get("revision"): row["status"] for row in current["orders"]}
        notes = {row["id"]: row["status"] for row in current["notes"]}
        if current["batch"] is not None or any(orders.get(revision) != WAITING for revision in batch["orders"]) \
                or any(notes.get(identity) != WAITING for identity in batch["notes"]):
            raise order.PlanError(text("batch_queue_changed", language))
        current["batch"] = batch

    _update(root, change)
