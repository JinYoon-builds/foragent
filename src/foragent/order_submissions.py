"""Publication-owned retained orders: one file per waiting order entry.

An entry keeps its plan only while it waits. Once its batch is published the
plan lives in Git and GitHub, so the entry file is deleted (D95: derive, don't
store). Legacy ``orders.jsonl`` rows stay readable for batches written before
this layout and are never appended to again. Flush migrates them: each row then
keeps only what Git and GitHub cannot know, and the original bytes stay in a kept
copy that ``fa order restore-journal`` writes back.
"""

from datetime import UTC, datetime
import hashlib
import json
import os
import time
from uuid import uuid4

from foragent import order_notes
from foragent.i18n import text
from foragent.order import PlanError
from foragent.state import StateStore

DIRECTORY = "order-submissions"
LEGACY = "orders.jsonl"
KEPT = "orders-legacy-kept"
# Fields Git and GitHub hold once an order is published; a migrated row drops them.
_COPIES = ("plan", "files")


def retain(plan: dict, repo: str, boundary: str, *, approve: bool,
           approve_keys: list[str] | None, approve_numbers: list[int] | None,
           language: str, files: dict[str, str | bytes] | None = None,
           approval: dict | None = None) -> None:
    if not boundary.strip():
        raise PlanError(text("order_submission_boundary", language))
    # Selection is evidence supplied by the caller, never a derived "approved" fact.
    # A future publisher must validate the retained plan and preread again.
    row = {"id": plan["id"], "plan": plan, "approve": approve,
           "approve_keys": approve_keys or [], "approve_numbers": approve_numbers or [],
           "boundary": boundary, "recorded_at": datetime.now(UTC).isoformat(), "status": "waiting"}
    if approval is not None:
        row["approval"] = approval  # The recorded approval it was checked against, shown when published.
    if files is not None:
        # Implicit Done when tests can outlive their caller's checkout files too.
        # Binary references already travel in the plan's base64 reference_files.
        row["files"] = {name: content for name, content in files.items() if isinstance(content, str)}
    try:
        _write(StateStore.for_repo(repo), row)
    except OSError as exc:
        raise PlanError(text("order_submission_io", language, error=exc)) from exc


def _write(store, row: dict, name: str | None = None) -> None:
    """Create one complete entry file atomically; no lock, no earlier byte rewritten."""
    # Names sort in retention order, so the latest revision of an order is the last one.
    name = name or f"{time.time_ns():020d}-{uuid4().hex}"
    _replace(store.root / DIRECTORY, f"{name}.json", json.dumps(row, ensure_ascii=False).encode("utf-8"))


def _replace(directory, name: str, data: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    partial = directory / f".{name}.partial"
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        if os.write(fd, data) != len(data):
            raise OSError("short write")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(partial, directory / name)


def _files(store) -> list[tuple]:
    directory = store.root / DIRECTORY
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    rows = []
    for path in paths:
        try:
            row = json.loads(path.read_bytes())
        except (FileNotFoundError, ValueError, UnicodeError):
            continue  # Deleted after publication meanwhile, or not a complete entry.
        if isinstance(row, dict):
            # A migrated legacy entry keeps the entry id its flush records already name.
            rows.append((path, {**row, "entry_id": row.get("entry_id", f"{DIRECTORY}:{path.stem}")}))
    return rows


def _migrated(records: list[dict]) -> bool:
    return any(row["kind"] == "migrated" for row in records)


def entries(store, records: list[dict] | None = None) -> list[dict]:
    """Every retained order entry, oldest first, each with its ``entry_id``."""
    from foragent import order_flush_journal as journal

    records = journal.entries(store) if records is None else records
    # After migration the legacy ledger is never read again, even if a copy reappears.
    legacy = [] if _migrated(records) else order_notes.entries(store, LEGACY, with_offsets=True)
    return legacy + [row for _path, row in _files(store)]


def revision(row) -> list:
    """An entry's durable retention order, including pre-migration byte offsets."""
    source, _, value = row["entry_id"].partition(":")
    return [0, int(value)] if source == LEGACY else [1, value]


def migrate_legacy(store) -> None:
    """Move each legacy ledger row into its own entry file, then retire the ledger.

    Callers hold the flush lock. Published rows keep only their order id, in the
    flush journal; waiting and selected rows keep their legacy entry id.
    """
    from foragent import order_flush_journal as journal

    if not (store.root / LEGACY).exists():
        return
    if not _migrated(journal.entries(store)):
        published = journal.published_entries(store)
        rows = order_notes.entries(store, LEGACY, with_offsets=True)
        for row in rows:
            if row["entry_id"] not in published:
                offset = int(row["entry_id"].partition(":")[2])
                # Zero-time names sort before every later entry, preserving revision order.
                # Fixed names make a rerun after an interruption rewrite, not duplicate.
                _write(store, row, name=f"{0:020d}-legacy-{offset:012d}")
        journal.append(store, "migrated", "legacy", ids={row["entry_id"]: row["id"] for row in rows})
    # Also slims a ledger left behind by a run that stopped right after its migration record.
    _slim_legacy(store)


def _slim(data: bytes) -> bytes:
    lines = data.split(b"\n")
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue  # Blank lines and interrupted tails stay as they are.
        if isinstance(row, dict) and any(key in row for key in _COPIES):
            kept = {key: value for key, value in row.items() if key not in _COPIES}
            lines[index] = json.dumps(kept, ensure_ascii=False).encode("utf-8")
    return b"\n".join(lines)


def _slim_legacy(store) -> None:
    """Drop plan copies from the retired ledger, keeping its original bytes first."""
    from foragent import order_flush_journal as journal

    path = store.root / LEGACY
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return
    slim = _slim(data)
    if slim == data:
        return
    digest = hashlib.sha256(data).hexdigest()
    changes = [row for row in journal.entries(store) if row["kind"] in {"slimmed", "restored"}]
    if changes and changes[-1]["kind"] == "restored" and changes[-1].get("kept") == digest:
        return  # A person restored these bytes on purpose; leave them.
    copy = store.root / KEPT / f"{digest}.jsonl"
    if not copy.exists():
        _replace(store.root / KEPT, copy.name, data)
    # The record comes first, so restore can always find the copy that replaced bytes.
    journal.append(store, "slimmed", "legacy", kept=digest)
    _replace(store.root, LEGACY, slim)


def restore_legacy(store) -> str | None:
    """Write the latest kept legacy ledger back byte for byte; None when nothing was slimmed."""
    from foragent import order_flush_journal as journal

    with journal.locked(store):
        slimmed = [row for row in journal.entries(store) if row["kind"] == "slimmed"]
        if not slimmed:
            return None
        digest = slimmed[-1]["kept"]
        _replace(store.root, LEGACY, (store.root / KEPT / f"{digest}.jsonl").read_bytes())
        journal.append(store, "restored", "legacy", kept=digest)
        return digest


def unselected(store, records: list[dict]) -> list[dict]:
    """Entries no flush batch has taken yet."""
    from foragent.order_flush_journal import selected_entries

    selected = selected_entries(records)
    return [row for row in entries(store, records) if row["entry_id"] not in selected]


def discard_published(store) -> None:
    """Delete each published entry's file; Git and GitHub hold those plans now."""
    from foragent.order_flush_journal import published_entries

    published = published_entries(store)
    for path, row in _files(store):
        if row["entry_id"] in published:
            path.unlink(missing_ok=True)


def pending(store) -> list[dict]:
    from foragent import order_flush_journal as journal

    records = journal.entries(store)
    published = journal.published_entries(store)
    rows = entries(store, records)
    ids = {**{row["entry_id"]: row["id"] for row in rows}, **journal.selected_ids(records)}
    # A published order is shown from the flush journal only, never from a retained plan copy.
    shown = [{"id": ids.get(entry), "boundary": row["boundary"], "entry_id": entry, "status": "published"}
             for row in records if row["kind"] == "selected"
             for entry in row["orders"] if entry in published]
    waiting = [row for row in rows if row["entry_id"] not in published]
    latest = {row["id"]: row for row in waiting}
    return shown + [{**row, "status": "waiting"} for row in waiting if row is latest[row["id"]]]


def run_restore(args, root) -> int:
    from foragent.commands import repo_store

    config, _repo, store = repo_store(root)
    try:
        digest = restore_legacy(store)
    except OSError as exc:
        raise PlanError(text("order_journal_restore_failed", config.language, error=exc)) from exc
    if args.json:
        print(json.dumps({"restored": digest is not None, "kept": digest}))
    else:
        print(text("order_journal_restored" if digest else "order_journal_restore_none", config.language))
    return 0
