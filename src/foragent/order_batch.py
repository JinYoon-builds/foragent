"""Waiting orders and planning notes, published together at a chosen boundary (D85).

The queue is local control state in the repository's ignored ``.foragent/order-batch.json``
(beside preread evidence), so waiting work never changes the shared checkout's content.
A boundary is a name the person or agent chooses (for example ``next-code-merge``);
``flush`` publishes everything waiting for it in one commit and push, then runs each
order's normal tracker publication. Every order keeps its own approval and preread
requirements. A batch commit is never undone: an interrupted flush finishes the one pending
batch before anything new (``order_batch_flush``); issues are found again by their markers.
"""

import base64
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from foragent import order
from foragent.config import Config
from foragent.i18n import text
from foragent.order_batch_notes import NOTE_FILES
from foragent.order_batch_queue import PUBLISHED, WAITING, _load, _update


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
        # A newer version of a still-waiting order replaces it; published history stays, and
        # so does a member of the pending batch, whose committed files still await its issues.
        members = set((state["batch"] or {}).get("orders", {}))
        state["orders"] = [row for row in state["orders"]
                           if not (row["id"] == plan["id"] and row["status"] != PUBLISHED
                                   and row["revision"] not in members)]
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


def _text(data: str | None) -> str | None:
    return None if data is None else base64.b64decode(data).decode("utf-8", errors="replace")


def pending(root: Path) -> dict:
    """Every queued order and note with its status; plan bodies stay in the queue file.

    A member of the pending batch shows that batch's status, derived from its one record.
    """
    state = _load(root)
    keys = ("id", "revision", "title", "boundary", "status", "approve", "approve_keys", "approve_numbers",
            "queued_at", "published_at", "parent")
    batch = state["batch"]
    status = None if batch is None else "committed" if batch.get("commit") else "writing"
    members = set() if batch is None else set(batch["orders"]) | set(batch["notes"])

    def row_view(row: dict, fields) -> dict:
        view = {key: row[key] for key in fields if key in row}
        if row.get("revision", row["id"]) in members and row["status"] != PUBLISHED:
            view["status"] = status
        return view

    return {"orders": [row_view(row, keys) for row in state["orders"]],
            "notes": [row_view(row, row) for row in state["notes"]],
            # The one pending batch: committed (with its sha) or still to commit, not yet published.
            "batch": None if batch is None else {
                "id": batch["id"], "boundary": batch["boundary"], "commit": batch.get("commit"),
                "pushed": batch.get("pushed", False), "status": status,
                "paths": sorted(batch["contents"]), "orders": sorted(batch["orders"]), "notes": batch["notes"],
                # Both versions of each path, for a person resolving an edit made meanwhile.
                "files": {name: {"before": _text(batch.get("before", {}).get(name)), "batch": _text(data)}
                          for name, data in sorted(batch["contents"].items())}}}


def flush(boundary: str, root: Path, config: Config, tracker, *, repo: str, git) -> dict:
    """Publish what waits for ``boundary`` in one commit and push (``order_batch_flush``)."""
    from foragent import order_batch_flush

    boundary = _boundary(boundary, config.language)
    return order_batch_flush.flush(boundary, root, config, tracker, repo=repo, git=git,
                                   unchanged=_unchanged_inputs)
