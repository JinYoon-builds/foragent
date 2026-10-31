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
               commit: str) -> None:
    """Record that the batch commit exists: later runs push and finish, never rewrite."""
    for revision, names in orders.items():
        _mark(root, "orders", {revision}, status=COMMITTED, commit=commit,
              blobs={name: heads[name] for name in names})
    _mark(root, "notes", set(notes), status=COMMITTED, commit=commit)


def _revalidate(root: Path, git) -> None:
    """A committed checkpoint whose commit left HEAD's history is undone, then waits again.

    Its files go back first (worktree and index, from the commit's undo record), so the
    retry starts from the checkout the batch began with; the record goes last.
    """
    rows = [row for kind in ("orders", "notes") for row in _load(root)[kind] if row["status"] == COMMITTED]
    commits = {row.get("commit") for row in rows}
    gone = {commit for commit in commits if not journal.landed(git, commit)}  # the recovery predicate
    directory = _directory(root)
    for commit in sorted(gone, key=str):
        if commit is not None:
            journal.undo_commit(directory, root, git, commit)

    def change(current: dict) -> None:
        for row in [*current["orders"], *current["notes"]]:
            if row["status"] == COMMITTED and row.get("commit") in gone:
                row["status"] = WAITING
                for field in ("commit", "blobs"):
                    row.pop(field, None)

    if gone:
        _update(root, change)
    journal.forget(directory, {commit for commit in commits - gone if commit is not None})
