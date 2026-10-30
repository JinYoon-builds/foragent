"""Workload records: one file per machine slot held outside a gate run (#991).

The process that holds the slot is the only writer: it adds its record after
admission and removes it on exit. Readers never change a record; a record whose
process is gone stops counting at once (capacity's liveness rule decides that)
and is left for the owner or later retention to remove.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from foragent.state import StateStore

WORKLOADS_DIR = "workloads"


@dataclass(frozen=True)
class Workload:
    path: Path
    pid: int
    kind: str
    issue: int | None = None
    role: str | None = None

    def label(self) -> str:
        parts = [self.kind]
        if self.role:
            parts.append(self.role)
        if self.issue is not None:
            parts.append(f"#{self.issue}")
        return " ".join(parts)


def directory(root: Path) -> Path:
    return root / WORKLOADS_DIR


def new_path(store: StateStore, pid: int) -> Path:
    """Name a record before it exists, so the caller's cleanup owns it from the start."""
    return directory(store.root) / f"{pid}-{secrets.token_hex(4)}.json"


def add(
    store: StateStore, path: Path, *, pid: int, kind: str, issue: int | None = None, role: str | None = None,
) -> None:
    """Write the record at ``path`` (from new_path) for a slot this process now holds.

    The caller's cleanup owns ``path`` before this runs, so an interruption anywhere
    in here, the rename included, still ends in its removal.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    value: dict[str, Any] = {
        "pid": pid, "kind": kind, "started_at": datetime.now(UTC).isoformat(),
    }
    if issue is not None:
        value["issue"] = issue
    if role is not None:
        value["role"] = role
    store._write_json(path, value)


def remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def read(root: Path) -> list[Workload]:
    """Every readable record under a store root; unreadable files hold nothing."""
    records = []
    for path in sorted(directory(root).glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            pid = value["pid"]
            if type(pid) is not int or pid <= 0:
                continue
            issue = value.get("issue")
            records.append(Workload(
                path, pid, str(value.get("kind") or "workload"),
                issue if type(issue) is int else None,
                str(value["role"]) if value.get("role") else None,
            ))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return records
