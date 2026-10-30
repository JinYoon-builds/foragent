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


def add(
    store: StateStore, *, pid: int, kind: str, issue: int | None = None, role: str | None = None,
) -> Path:
    """Write one record for a slot this process now holds; the caller removes it."""
    folder = directory(store.root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{pid}-{secrets.token_hex(4)}.json"
    value: dict[str, Any] = {
        "pid": pid, "kind": kind, "started_at": datetime.now(UTC).isoformat(),
    }
    if issue is not None:
        value["issue"] = issue
    if role is not None:
        value["role"] = role
    store._write_json(path, value)
    return path


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
