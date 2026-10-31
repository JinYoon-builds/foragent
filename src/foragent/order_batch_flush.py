"""`fa order flush`: publish what waits for a boundary in one commit and one push (D85).

Rules, in order of the steps:

- Flush runs only on the base branch; elsewhere it refuses before touching anything.
- At most one batch is pending (committed, not yet published). A flush first finishes it:
  finds its commit (recorded sha, else its batch id in a commit message since
  ``origin/<base>``), recommits the same recorded contents when the commit is gone,
  rebases it onto a moved base, pushes, then publishes each member order's issues.
  Only then is a new batch built from what waits.
- A batch commit is never undone, and only the batch's own listed paths are written or
  committed (an empty list commits nothing). Contents are fixed in memory when the batch
  is built, so every retry writes the same bytes and notes never repeat.
"""

import base64
import uuid
from datetime import UTC, datetime
from pathlib import Path

from foragent import order
from foragent.config import Config
from foragent.i18n import text
from foragent.order_batch_notes import _note_files, _write_notes
from foragent.order_batch_queue import (
    COMMITTED,
    PUBLISHED,
    WAITING,
    _load,
    _lock,
    _mark,
    _set_batch,
)
from foragent.order_files import check_destinations


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _encoded(content: str | bytes) -> str:
    return base64.b64encode(content.encode("utf-8") if isinstance(content, str) else content).decode("ascii")


def _approval(entry: dict) -> dict:
    return {"approve": entry["approve"], "approve_keys": entry["approve_keys"],
            "approve_numbers": entry["approve_numbers"]}


def marker(batch: dict) -> str:
    """The commit message, whose batch id finds the commit after a crash."""
    return f"Foragent batch {batch['boundary']} {batch['id']}"


def _order_rows(root: Path, revisions) -> list[dict]:
    rows = {row.get("revision"): row for row in _load(root)["orders"]}
    return [rows[revision] for revision in revisions if revision in rows]


def _write(batch: dict, root: Path, config: Config, git) -> None:
    """Write exactly the batch's recorded bytes to its listed paths (idempotent)."""
    contents = {name: base64.b64decode(data) for name, data in batch["contents"].items()}
    for row in _order_rows(root, batch["orders"]):
        files = {name: contents[name] for name in batch["orders"][row["revision"]]}
        order.write_order_files(row["plan"], root, config, git, files, None)
    _write_notes(root, {name: contents[name].decode("utf-8") for name in batch["note_paths"]})


def _commit(batch: dict, root: Path, config: Config, git) -> str:
    """The batch's commit in HEAD's history, making it from the recorded contents if absent."""
    sha = batch.get("commit")
    if sha is not None and git.is_ancestor(sha, "HEAD"):
        return sha
    sha = git.batch_commit(marker(batch), config.base_branch)
    if sha is None:
        _write(batch, root, config, git)
        paths = list(batch["contents"])
        git.ensure_tracked(paths)
        changed = git.uncommitted_paths(paths)
        if changed:  # an empty list commits nothing
            git.commit_paths(changed, marker(batch))
        sha = git.head_sha()
    blobs = git.blob_shas(sha, list(batch["contents"]))
    if missing := sorted(name for name, blob in blobs.items() if blob == "missing"):
        raise order.PlanError(f"published commit is missing files: {', '.join(missing)}")
    _set_batch(root, commit=sha)
    for revision, names in batch["orders"].items():
        _mark(root, "orders", {revision}, status=COMMITTED, commit=sha, blobs={name: blobs[name] for name in names})
    _mark(root, "notes", set(batch["notes"]), status=COMMITTED, commit=sha)
    return sha


def _finish(batch: dict, root: Path, config: Config, tracker, repo: str, git) -> dict:
    """Commit (if needed), push and publish the pending batch; it is dropped only when done."""
    if not batch.get("pushed"):
        _commit(batch, root, config, git)
        if git.rebase_onto_base(config.base_branch):
            _set_batch(root, commit=None)  # the rebase gave the commit a new sha
            _commit(_load(root)["batch"], root, config, git)
        git.push_base(config.base_branch)
        _set_batch(root, pushed=True)
    _mark(root, "notes", set(batch["notes"]), status=PUBLISHED, published_at=_now())
    parents: dict[str, int | None] = {}
    for row in _order_rows(root, batch["orders"]):
        if row["status"] == PUBLISHED:
            continue  # a resumed batch: this order's issues were already published
        report = order.publish(row["plan"], root, config, tracker, repo=repo, git=git,
                               published_blobs=row["blobs"], **_approval(row))
        parents[row["id"]] = report.parent
        _mark(root, "orders", {row["revision"]}, status=PUBLISHED, published_at=_now(), parent=report.parent)
    _set_batch(root, batch=None)
    return {"orders": list(parents), "notes": sorted(batch["notes"]), "parents": parents}


def _build(boundary: str, root: Path, config: Config, tracker, repo: str, git, unchanged) -> dict | None:
    """Fix one new batch from what waits for ``boundary``: contents decided, nothing written yet."""
    state = _load(root)
    orders = [row for row in state["orders"] if row["status"] == WAITING and row["boundary"] == boundary]
    notes = [row for row in state["notes"] if row["status"] == WAITING and row["boundary"] == boundary]
    if not orders and not notes:
        return None
    prepared = []
    for entry in orders:
        # Every order's own checks pass before anything is written for any of them.
        order.publish(entry["plan"], root, config, tracker, repo=repo, check_only=True, **_approval(entry))
        files = order.validate_plan(entry["plan"], root, config)
        unchanged(entry, files, config.language)
        prepared.append((entry, files))
    combined: dict[str, str | bytes] = {}
    for _entry, files in prepared:
        for name, content in files.items():
            if name in combined and combined[name] != content:
                raise order.PlanError(text("batch_path_conflict", config.language, path=name))
            combined[name] = content
    git.update_base(config.base_branch)
    check_destinations(root, combined)
    note_contents = _note_files(root, notes, config.language)  # may refuse; nothing written yet
    batch = {"id": uuid.uuid4().hex, "boundary": boundary, "commit": None, "pushed": False,
             "contents": {name: _encoded(data) for name, data in {**combined, **note_contents}.items()},
             "orders": {entry["revision"]: sorted(files) for entry, files in prepared},
             "notes": [row["id"] for row in notes], "note_paths": sorted(note_contents)}
    _set_batch(root, batch=batch)
    return batch


def flush(boundary: str, root: Path, config: Config, tracker, *, repo: str, git, unchanged) -> dict:
    """Finish the pending batch, then publish what waits for ``boundary`` as one new batch."""
    if git.current_branch() != config.base_branch:
        # Checked first: off the base branch nothing is fetched, written or changed in the queue.
        raise order.PlanError(text("batch_wrong_branch", config.language, branch=config.base_branch))
    result = {"boundary": boundary, "orders": [], "notes": [], "parents": {}}
    with _lock(root, "order-batch-flush.lock"), git.publish_lock():
        pending = _load(root)["batch"]
        if pending is not None:
            done = _finish(pending, root, config, tracker, repo, git)
            for key in ("orders", "notes"):
                result[key] += done[key]
            result["parents"].update(done["parents"])
        batch = _build(boundary, root, config, tracker, repo, git, unchanged)
        if batch is not None:
            done = _finish(batch, root, config, tracker, repo, git)
            for key in ("orders", "notes"):
                result[key] += done[key]
            result["parents"].update(done["parents"])
    return result
