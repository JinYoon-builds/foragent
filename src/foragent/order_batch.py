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
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from foragent import order, order_references
from foragent.config import Config, load_config
from foragent.git import GitError
from foragent.i18n import text
from foragent.order_files import check_destinations, check_write
from foragent.paths import repo_path
from foragent.state_locks import _file_lock

QUEUE_FILE = "order-batch.json"
NOTE_FILES = {"decision": "docs/decisions.md", "idea": "docs/ideas.md"}
WAITING, PUBLISHED = "waiting", "published"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _directory(root: Path) -> Path:
    """The ignored local control directory; a planted symlink must not redirect it."""
    directory = order_references.real_directory(root, ".foragent")
    ignore = directory / ".gitignore"
    if ignore.is_symlink():
        raise order.PlanError(f"{ignore} must be a regular file, not a symlink")
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
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
          approve_numbers: list[int] | None = None) -> dict:
    """Check an order exactly as publication would, then keep its content waiting."""
    boundary = _boundary(boundary, config.language)
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


def note(root: Path, kind: str, note_text: str, boundary: str, language: str) -> dict:
    """Keep a planning decision or idea for the chosen boundary."""
    boundary = _boundary(boundary, language)
    if kind not in NOTE_FILES:
        raise order.PlanError(text("batch_bad_kind", language, kinds=", ".join(NOTE_FILES)))
    if not isinstance(note_text, str) or not note_text.strip():
        raise order.PlanError(text("batch_empty_note", language))
    # A random id: the same text recorded twice in one second is still two notes.
    entry = {"id": uuid.uuid4().hex, "kind": kind, "text": note_text.strip(), "boundary": boundary,
             "status": WAITING, "queued_at": _now()}
    _update(root, lambda state: state["notes"].append(entry))
    return entry


def pending(root: Path) -> dict:
    """Every queued order and note with its status; plan bodies stay in the queue file."""
    state = _load(root)
    keys = ("id", "revision", "title", "boundary", "status", "approve", "approve_keys", "approve_numbers",
            "queued_at", "published_at", "parent")
    return {"orders": [{key: row[key] for key in keys if key in row} for row in state["orders"]],
            "notes": [dict(row) for row in state["notes"]]}


def _note_marker(entry: dict) -> str:
    return f"<!-- foragent:note {entry['id']} -->"


def _note_files(root: Path, notes: list[dict]) -> dict[str, str]:
    """Each note file with the waiting notes it does not already contain appended once."""
    contents: dict[str, str] = {}
    for entry in notes:
        name = NOTE_FILES[entry["kind"]]
        if name not in contents:
            path = repo_path(root, name)
            contents[name] = path.read_text(encoding="utf-8") if path.is_file() else ""
        if _note_marker(entry) not in contents[name]:
            current = contents[name]
            separator = "" if not current or current.endswith("\n\n") else "\n" if current.endswith("\n") else "\n\n"
            contents[name] = f"{current}{separator}{_note_marker(entry)}\n{entry['text']}\n"
    return contents


def _write_notes(root: Path, contents: dict[str, str]) -> None:
    for name, content in contents.items():
        path = repo_path(root, name)
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            continue
        own_root = PurePosixPath(name).parent.as_posix()
        check_write(root, name, own_root)
        order_references.real_directory(root, own_root)
        check_write(root, name, own_root)
        path.write_text(content, encoding="utf-8")


def _check_new_lanes(plans: list[dict], config: Config) -> None:
    """Orders creating the same lane must agree on it, or the second would fail after the commit."""
    seen: dict[str, dict] = {}
    for plan in plans:
        for lane in plan.get("new_lanes", []):
            if lane["name"] in config.lanes:
                continue  # publish's own check compares it with the existing lane
            if seen.setdefault(lane["name"], lane) != lane:
                raise order.PlanError(text("batch_lane_conflict", config.language, lane=lane["name"]))


def flush(boundary: str, root: Path, config: Config, tracker, *, repo: str, git,
          manager=None) -> dict:
    """Publish what waits for ``boundary``: one commit and push, then each order's issues."""
    boundary = _boundary(boundary, config.language)
    with _lock(root, "order-batch-flush.lock"):
        state = _load(root)
        orders = [row for row in state["orders"] if row["status"] != PUBLISHED and row["boundary"] == boundary]
        notes = [row for row in state["notes"] if row["status"] != PUBLISHED and row["boundary"] == boundary]
        if not orders and not notes:
            return {"boundary": boundary, "orders": [], "notes": [], "parents": {}}
        prepared = []
        for entry in orders:
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
        _check_new_lanes([entry["plan"] for entry, _ in prepared], config)
        if git.current_branch() != config.base_branch:
            raise GitError(f"order flush must run on the base branch {config.base_branch!r}")
        with git.publish_lock():
            git.update_base(config.base_branch)
            check_destinations(root, combined)
            for entry, files in prepared:
                order.write_order_files(entry["plan"], root, config, git, files, manager)
                if any(lane["name"] not in config.lanes for lane in entry["plan"].get("new_lanes", [])):
                    # Later orders and the issue pass see the lane this order just created.
                    config = replace(config, lanes=load_config(root).lanes)
            note_contents = _note_files(root, notes)
            _write_notes(root, note_contents)
            title = f"Foragent batch {boundary}: {len(orders)} order(s), {len(notes)} note(s)"
            blobs = order.commit_and_push(config, git, {**combined, **note_contents}, title)
        done_at, published = _now(), {row["id"] for row in notes}

        def mark_notes(current: dict) -> None:
            # Notes are files only, so the pushed commit has published them.
            for row in current["notes"]:
                if row["id"] in published:
                    row.update(status=PUBLISHED, published_at=done_at)

        _update(root, mark_notes)
        parents: dict[str, int | None] = {}
        for entry, files in prepared:
            report = order.publish(entry["plan"], root, config, tracker, repo=repo, git=git, manager=manager,
                                   published_blobs={name: blobs[name] for name in files}, **_approval(entry))
            parents[entry["id"]] = report.parent

            def mark(current: dict, revision=entry["revision"], parent=report.parent) -> None:
                for row in current["orders"]:
                    if row.get("revision") == revision and row["status"] != PUBLISHED:
                        row.update(status=PUBLISHED, published_at=_now(), parent=parent)

            _update(root, mark)
        return {"boundary": boundary, "orders": [entry["id"] for entry, _ in prepared],
                "notes": sorted(published), "parents": parents}
