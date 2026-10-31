"""Waiting orders and planning notes, published together at a chosen boundary (D85).

The queue is local control state in the repository's ignored ``.foragent/order-batch.json``
(beside preread evidence), so waiting work never changes the shared checkout's content.
A boundary is a name the person or agent chooses (for example ``next-code-merge``);
``flush`` publishes everything waiting for it in one commit and push, then runs each
order's normal tracker publication. Every order keeps its own approval and preread
requirements. An interrupted flush is finished or undone from its journal's recorded
commit (``order_batch_journal``); issues are found again by their existing markers.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from foragent import order
from foragent import order_batch_journal as journal
from foragent.config import Config
from foragent.git import GitError
from foragent.i18n import text
from foragent.order_batch_journal import _directory, _lock
from foragent.order_batch_notes import COMMITTED, NOTE_FILES, _note_files, _write_notes
from foragent.order_batch_queue import (
    PUBLISHED,
    WAITING,
    _committed,
    _load,
    _mark,
    _revalidate,
    _update,
)
from foragent.order_files import check_destinations


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _boundary(boundary: str, language: str) -> str:
    if not isinstance(boundary, str) or not boundary.strip() or boundary.splitlines() != [boundary]:
        raise order.PlanError(text("batch_bad_boundary", language))
    return boundary.strip()


def _digests(files: dict[str, str | bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(content.encode("utf-8") if isinstance(content, str) else content).hexdigest()
            for name, content in sorted(files.items())}


def _unchanged_inputs(entry: dict, files: dict[str, str | bytes], language: str) -> None:
    """Refuse an order whose published files differ from those checked when it was deferred."""
    queued, current = entry.get("files"), _digests(files)
    if queued == current:
        return
    changed = sorted(name for name in set(queued or {}) | set(current)
                     if (queued or {}).get(name) != current.get(name))
    raise order.PlanError(text("batch_input_changed", language, id=entry["id"],
                               paths=", ".join(changed) or "?"))


def _approval(entry: dict) -> dict:
    return {"approve": entry["approve"], "approve_keys": entry["approve_keys"],
            "approve_numbers": entry["approve_numbers"]}


def defer(plan: dict, boundary: str, root: Path, config: Config, tracker, *, repo: str,
          approve: bool = False, approve_keys: list[str] | None = None,
          approve_numbers: list[int] | None = None) -> dict:
    """Check an order exactly as publication would, then keep its content waiting.

    An order that adds lanes (``new_lanes``) is never queued: it publishes immediately,
    as before batching, so a batch never creates or recovers lane configuration.
    """
    boundary = _boundary(boundary, config.language)
    if plan.get("new_lanes"):
        raise order.PlanError(text("batch_lanes_publish_now", config.language))
    # Each queued version has its own revision; a time or plan id cannot tell versions apart.
    entry = {"id": plan["id"], "revision": uuid.uuid4().hex, "title": plan["title"],
             "boundary": boundary, "status": WAITING,
             "approve": approve, "approve_keys": list(approve_keys or []),
             "approve_numbers": list(approve_numbers or []), "queued_at": _now(), "plan": plan}
    # The same checks as publication, including a clean preread for approved issues.
    order.publish(plan, root, config, tracker, repo=repo, check_only=True, **_approval(entry))
    # Approval covers the files as they are now, including Done when tests read from disk.
    entry["files"] = _digests(order.validate_plan(plan, root, config))

    def add(state: dict) -> None:
        # A newer version of a still-waiting order replaces it; published history stays.
        state["orders"] = [row for row in state["orders"]
                           if not (row["id"] == plan["id"] and row["status"] != PUBLISHED)]
        state["orders"].append(entry)

    _update(root, add)
    return entry


def note(root: Path, kind: str, note_text: str, boundary: str, language: str,
         replace: str | None = None) -> dict:
    """Keep a planning decision or idea for the chosen boundary."""
    boundary = _boundary(boundary, language)
    if kind not in NOTE_FILES:
        raise order.PlanError(text("batch_bad_kind", language, kinds=", ".join(NOTE_FILES)))
    if not isinstance(note_text, str) or not note_text.strip():
        raise order.PlanError(text("batch_empty_note", language))
    if replace is not None and kind != "rule":
        raise order.PlanError(text("batch_replace_rule_only", language))
    if kind == "rule" and any(value is not None and (not value.strip() or len(value.strip().splitlines()) != 1)
                              for value in (note_text, replace)):
        raise order.PlanError(text("batch_rule_one_line", language))
    # A random id: the same text recorded twice in one second is still two notes.
    entry = {"id": uuid.uuid4().hex, "kind": kind, "text": note_text.strip(), "boundary": boundary,
             "status": WAITING, "queued_at": _now()}
    if replace is not None:
        entry["replace"] = replace.strip()
    _update(root, lambda state: state["notes"].append(entry))
    return entry


def pending(root: Path) -> dict:
    """Every queued order and note with its status; plan bodies stay in the queue file."""
    state = _load(root)
    keys = ("id", "revision", "title", "boundary", "status", "approve", "approve_keys", "approve_numbers",
            "queued_at", "published_at", "parent")
    return {"orders": [{key: row[key] for key in keys if key in row} for row in state["orders"]],
            "notes": [dict(row) for row in state["notes"]]}


def _settle(root: Path, git, language: str) -> bool:
    """Undo or finish a flush that stopped mid-write; True when there was one."""
    directory = _directory(root)
    if not (directory / journal.JOURNAL_FILE).exists():
        return False
    # A flush of any boundary that stopped mid-write is settled before anything reads files.
    with git.publish_lock():
        journal.recover(directory, root, git, lambda *facts: _committed(root, *facts), language)
    return True


def recover(root: Path, git, language: str = "en") -> bool:
    """Settle an interrupted flush using only the repository, Git and the journal.

    It reads no configuration, because the interrupted write may have damaged
    .foragent.yml; callers run it before loading configuration for a flush.
    """
    with _lock(root, "order-batch-flush.lock"):
        settled = _settle(root, git, language)
        _revalidate(root, git)
        return settled


def _require_base(git, config: Config) -> None:
    if git.current_branch() != config.base_branch:
        raise GitError(f"order flush must run on the base branch {config.base_branch!r}")


def flush(boundary: str, root: Path, config: Config, tracker, *, repo: str, git) -> dict:
    """Publish what waits for ``boundary``: one commit and push, then each order's issues.

    Status moves waiting -> committed (the local commit exists) -> published. Before
    writing, the flush journals every path it will write; recovery finishes or undoes an
    interrupted flush from the commit shas it recorded (``order_batch_journal``).
    """
    boundary = _boundary(boundary, config.language)
    with _lock(root, "order-batch-flush.lock"):
        directory = _directory(root)
        _settle(root, git, config.language)
        _revalidate(root, git)
        state = _load(root)
        orders = [row for row in state["orders"] if row["status"] != PUBLISHED and row["boundary"] == boundary]
        notes = [row for row in state["notes"] if row["status"] != PUBLISHED and row["boundary"] == boundary]
        if not orders and not notes:
            return {"boundary": boundary, "orders": [], "notes": [], "parents": {}}
        prepared = []
        for entry in orders:
            if entry["status"] == COMMITTED:
                continue  # its files are in a local commit; later edits to them must stay
            # Every order's own checks pass before anything is written for any of them.
            order.publish(entry["plan"], root, config, tracker, repo=repo, check_only=True, **_approval(entry))
            files = order.validate_plan(entry["plan"], root, config)
            _unchanged_inputs(entry, files, config.language)
            prepared.append((entry, files))
        combined: dict[str, str | bytes] = {}
        for entry, files in prepared:
            for name, content in files.items():
                if name in combined and combined[name] != content:
                    raise order.PlanError(text("batch_path_conflict", config.language, path=name))
                combined[name] = content
        _require_base(git, config)
        with git.publish_lock():
            git.update_base(config.base_branch)
            check_destinations(root, combined)
            applying = [row for row in notes if row["status"] != COMMITTED]
            if not prepared and not applying:
                # Only committed work is left: push the recorded commit. Nothing is written,
                # staged or committed, so unrelated checkout changes stay out.
                git.push_base(config.base_branch)
            else:
                note_contents = _note_files(root, applying, config.language)  # may refuse; nothing written yet
                planned = {**combined, **note_contents}
                written = {"orders": {entry["revision"]: sorted(files) for entry, files in prepared},
                           "notes": [row["id"] for row in applying]}
                journal.write(directory, root, planned, **written, branch=config.base_branch, base=git.head_sha())

                def committed(blobs: dict[str, str]) -> None:
                    # The commit exists; only now do its order files and notes reach the checkout.
                    for entry, files in prepared:
                        order.write_files_now(entry["plan"], root, config, files)
                    _write_notes(root, note_contents)
                    # With nothing to commit, HEAD already holds the batch; record it the same way.
                    journal.batch_done(directory, git.head_sha())
                    _committed(root, written["orders"], written["notes"], blobs, git.head_sha())
                    journal.clear(directory)

                try:
                    for entry, files in prepared:
                        # Destination checks only: no order file is in the checkout before the commit.
                        order.write_order_files(entry["plan"], root, config, git, files, None, write_files=False)
                    title = f"Foragent batch {boundary}: {len(prepared)} order(s), {len(applying)} note(s)"
                    # Exactly the listed paths, committed from memory: the journal can undo every one.
                    contents = {name: data.encode("utf-8") if isinstance(data, str) else data
                                for name, data in planned.items()}
                    order.commit_and_push(config, git, planned, title, after_commit=committed, contents=contents,
                                          record=lambda sha: journal.batch_done(directory, sha))
                except BaseException:
                    # Undo an uncommitted write now, so waiting work never stays in the checkout.
                    journal.recover(directory, root, git, lambda *facts: _committed(root, *facts), config.language)
                    _revalidate(root, git)
                    raise
        published = {row["id"] for row in notes}
        _mark(root, "notes", published, status=PUBLISHED, published_at=_now())
        parents: dict[str, int | None] = {}
        for entry in orders:
            current = next((row for row in _load(root)["orders"] if row.get("revision") == entry["revision"]), None)
            if current is None or current["status"] != COMMITTED:
                continue  # replaced while flushing; the newer version waits for its own flush
            report = order.publish(current["plan"], root, config, tracker, repo=repo, git=git,
                                   published_blobs=current["blobs"], **_approval(current))
            parents[entry["id"]] = report.parent
            _mark(root, "orders", {entry["revision"]}, status=PUBLISHED, published_at=_now(), parent=report.parent)
        return {"boundary": boundary, "orders": list(parents), "notes": sorted(published), "parents": parents}
