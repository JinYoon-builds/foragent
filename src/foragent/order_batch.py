"""Waiting orders and planning notes, published together at a chosen boundary (D85).

The queue is local control state in the repository's ignored ``.foragent/order-batch.json``
(beside preread evidence), so waiting work never changes the shared checkout's content. A boundary is a name the person or agent chooses
(for example ``next-code-merge``); ``flush`` publishes everything waiting for it in one
commit and push, then runs each order's normal tracker publication. Every order keeps
its own approval and preread requirements, and every step is safe to repeat after an
interruption: notes carry an id marker in their file, orders reuse their issue markers.
"""

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from foragent import order
from foragent import order_batch_journal as journal
from foragent.config import Config, load_config
from foragent.git import GitError
from foragent.i18n import text
from foragent.order_batch_journal import _directory, _lock
from foragent.order_batch_notes import COMMITTED, NOTE_FILES, _note_files, _write_notes
from foragent.order_files import check_destinations

QUEUE_FILE = "order-batch.json"
WAITING, PUBLISHED = "waiting", "published"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
          approve_numbers: list[int] | None = None, manager=None) -> dict:
    """Check an order exactly as publication would, then keep its content waiting."""
    boundary = _boundary(boundary, config.language)
    # Each queued version has its own revision; a time or plan id cannot tell versions apart.
    entry = {"id": plan["id"], "revision": uuid.uuid4().hex, "title": plan["title"],
             "boundary": boundary, "status": WAITING,
             "approve": approve, "approve_keys": list(approve_keys or []),
             "approve_numbers": list(approve_numbers or []), "queued_at": _now(), "plan": plan}
    # The same checks as publication, including a clean preread for approved issues.
    order.publish(plan, root, config, tracker, repo=repo, check_only=True, manager=manager, **_approval(entry))
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


def _check_new_lanes(plans: list[dict], config: Config) -> None:
    """Orders creating the same lane must agree on it, or the second would fail after the commit."""
    seen: dict[str, dict] = {}
    for plan in plans:
        for lane in plan.get("new_lanes", []):
            if lane["name"] in config.lanes:
                continue  # publish's own check compares it with the existing lane
            if seen.setdefault(lane["name"], lane) != lane:
                raise order.PlanError(text("batch_lane_conflict", config.language, lane=lane["name"]))


def _mark(root: Path, kind: str, revisions: set[str], **fields) -> None:
    def change(current: dict) -> None:
        for row in current[kind]:
            if row.get("revision", row["id"]) in revisions and row["status"] != PUBLISHED:
                row.update(fields)

    _update(root, change)


def _committed(root: Path, orders: dict[str, list[str]], notes: list[str], heads: dict[str, str]) -> None:
    """Record that the batch commit exists: later runs push and finish, never rewrite."""
    for revision, names in orders.items():
        _mark(root, "orders", {revision}, status=COMMITTED, blobs={name: heads[name] for name in names})
    _mark(root, "notes", set(notes), status=COMMITTED)


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
        return _settle(root, git, language)


def _require_base(git, config: Config) -> None:
    if git.current_branch() != config.base_branch:
        raise GitError(f"order flush must run on the base branch {config.base_branch!r}")


def flush(boundary: str, root: Path, config: Config, tracker, *, repo: str, git,
          manager=None) -> dict:
    """Publish what waits for ``boundary``: one commit and push, then each order's issues.

    Status moves waiting -> committed (the local commit exists) -> published. Before
    writing, the flush journals every file's earlier bytes; an interrupted write is undone
    or, when HEAD shows it committed, recorded as committed (``order_batch_journal``).
    """
    boundary = _boundary(boundary, config.language)
    with _lock(root, "order-batch-flush.lock"):
        directory = _directory(root)
        if _settle(root, git, config.language):
            # Recovery may have undone a lane in .foragent.yml; the caller's config predates it.
            config = replace(config, lanes=load_config(root).lanes)
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
            order.publish(entry["plan"], root, config, tracker, repo=repo, check_only=True, manager=manager,
                          **_approval(entry))
            files = order.validate_plan(entry["plan"], root, config)
            _unchanged_inputs(entry, files, config.language)
            prepared.append((entry, files))
        combined: dict[str, str | bytes] = {}
        for entry, files in prepared:
            for name, content in files.items():
                if name in combined and combined[name] != content:
                    raise order.PlanError(text("batch_path_conflict", config.language, path=name))
                combined[name] = content
        _check_new_lanes([entry["plan"] for entry, _ in prepared], config)
        _require_base(git, config)
        with git.publish_lock():
            git.update_base(config.base_branch)
            check_destinations(root, combined)
            applying = [row for row in notes if row["status"] != COMMITTED]
            note_contents = _note_files(root, applying, config.language)  # may refuse; nothing written yet
            planned = {**combined, **note_contents}
            written = {"orders": {entry["revision"]: sorted(files) for entry, files in prepared},
                       "notes": [row["id"] for row in applying]}
            creating = [lane for entry, _ in prepared for lane in entry["plan"].get("new_lanes", [])
                        if lane["name"] not in config.lanes]
            journal.write(directory, root, planned, **written, branch=config.base_branch,
                          lanes=journal.lane_writes(root, creating))

            def committed(blobs: dict[str, str]) -> None:
                _committed(root, written["orders"], written["notes"], blobs)
                journal.clear(directory)

            try:
                for entry, files in prepared:
                    order.write_order_files(entry["plan"], root, config, git, files, manager,
                                            lane_done=lambda name: journal.lane_done(directory, root, name))
                    if any(lane["name"] not in config.lanes for lane in entry["plan"].get("new_lanes", [])):
                        # Later orders and the issue pass see the lane this order just created.
                        config = replace(config, lanes=load_config(root).lanes)
                _write_notes(root, note_contents)
                title = f"Foragent batch {boundary}: {len(prepared)} order(s), {len(applying)} note(s)"
                order.commit_and_push(config, git, planned, title, after_commit=committed)
            except BaseException:
                # Undo an uncommitted write now, so waiting work never stays in the checkout.
                journal.recover(directory, root, git, lambda *facts: _committed(root, *facts), config.language)
                raise
        published = {row["id"] for row in notes}
        _mark(root, "notes", published, status=PUBLISHED, published_at=_now())
        parents: dict[str, int | None] = {}
        for entry in orders:
            current = next((row for row in _load(root)["orders"] if row.get("revision") == entry["revision"]), None)
            if current is None or current["status"] != COMMITTED:
                continue  # replaced while flushing; the newer version waits for its own flush
            if config.lanes.keys() != load_config(root).lanes.keys():
                config = replace(config, lanes=load_config(root).lanes)
            report = order.publish(current["plan"], root, config, tracker, repo=repo, git=git, manager=manager,
                                   published_blobs=current["blobs"], **_approval(current))
            parents[entry["id"]] = report.parent
            _mark(root, "orders", {entry["revision"]}, status=PUBLISHED, published_at=_now(), parent=report.parent)
        return {"boundary": boundary, "orders": list(parents), "notes": sorted(published), "parents": parents}
