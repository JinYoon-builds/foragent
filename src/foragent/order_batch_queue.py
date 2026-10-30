"""The local order-batch queue file and its committed checkpoints (D85, #1043)."""

import json
import os
import tempfile
from pathlib import Path

from foragent import order
from foragent import order_batch_journal as journal
from foragent.order_batch_journal import _directory, _lock
from foragent.order_batch_notes import COMMITTED

QUEUE_FILE = "order-batch.json"
WAITING, PUBLISHED = "waiting", "published"


def _load(root: Path) -> dict:
    path = root / ".foragent" / QUEUE_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if not path.is_symlink() else {}
    except FileNotFoundError:
        value = {}
    except ValueError as exc:
        raise order.PlanError(f"{path} is not valid JSON") from exc
    value = value if isinstance(value, dict) else {}
    return {"orders": list(value.get("orders", [])), "notes": list(value.get("notes", []))}


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


def _committed(root: Path, orders: dict[str, list[str]], notes: list[str], heads: dict[str, str],
               commit: str, token: str) -> None:
    """Record that the batch commit exists: later runs push and finish, never rewrite."""
    for revision, names in orders.items():
        _mark(root, "orders", {revision}, status=COMMITTED, commit=commit, token=token,
              blobs={name: heads[name] for name in names})
    _mark(root, "notes", set(notes), status=COMMITTED, commit=commit, token=token)


def _revalidate(root: Path, git) -> None:
    """A committed checkpoint whose commit left HEAD's history waits again, files and all."""
    found: dict[tuple, bool] = {}

    def change(current: dict) -> None:
        for row in [*current["orders"], *current["notes"]]:
            if row["status"] != COMMITTED:
                continue
            key = (row.get("commit"), row.get("token"))
            if key not in found:
                found[key] = journal.commit_of(git, *key) is not None
            if not found[key]:
                row["status"] = WAITING
                for field in ("commit", "token", "blobs"):
                    row.pop(field, None)

    if any(row["status"] == COMMITTED for row in [*_load(root)["orders"], *_load(root)["notes"]]):
        _update(root, change)
