"""Atomic local snapshots and run records, independent of GitHub.

This module is the stable facade: concern modules hold the implementation,
and every original name and StateStore method stays importable from here.
"""

import fcntl  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import tempfile  # noqa: F401
import threading  # noqa: F401
from collections.abc import Callable, Iterable, Iterator  # noqa: F401
from contextlib import contextmanager  # noqa: F401
from copy import deepcopy  # noqa: F401
from dataclasses import asdict, dataclass, field, replace  # noqa: F401
from datetime import UTC, datetime, timedelta  # noqa: F401
from pathlib import Path
from typing import IO, Any  # noqa: F401

from foragent.records import load_record, write_back  # noqa: F401
from foragent.state_base import StateBase
from foragent.state_issues import (  # noqa: F401
    REFRESH_INTERVAL,
    IssueSnapshotMixin,
    _forget_reads,
    observed_issue_facts,
)
from foragent.state_local import LocalRecordsMixin
from foragent.state_locks import _TICK_LOCKS_HELD, StateLocksMixin, _held_tick_locks  # noqa: F401
from foragent.state_records import Approval, Flag, InspectState, Run  # noqa: F401
from foragent.state_refresh import IssueRefreshMixin
from foragent.state_github import GithubCallsMixin
from foragent.state_logs import LogRetentionMixin
from foragent.state_runs import RunRecordsMixin

# The <state>/logs/ size cap; prune_logs deletes the oldest issue logs above it.
LOG_LIMIT_BYTES = 500 * 1024 * 1024


class StateStore(
    IssueSnapshotMixin, IssueRefreshMixin, RunRecordsMixin, GithubCallsMixin, LocalRecordsMixin,
    LogRetentionMixin, StateLocksMixin, StateBase,
):
    @classmethod
    def for_repo(cls, repo: str, *, now: Callable[[], datetime] | None = None) -> "StateStore":
        return cls(Path.home() / ".local/share/foragent" / repo.replace("/", "__"), now)

    # Heartbeat file stem for each side; each write overwrites one small file.
    HEARTBEATS = {"orchestrator": "orch", "gate": "gate"}

    def write_heartbeat(self, side: str, at: datetime, *, session: dict | None = None) -> None:
        """Overwrite one side's heartbeat with ``at`` (no lock: one atomic replace).

        ``session`` is the writer's Claude session ``{pid, start_time}``, if known.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        record: dict = {"at": at.isoformat()}
        if session is not None:
            record["session"] = session
        self._write_json(self.root / f"{self.HEARTBEATS[side]}-heartbeat.json", record)

    def heartbeat_session(self, side: str) -> dict | None:
        """The Claude session recorded in one side's heartbeat, or None."""
        try:
            session = json.loads(
                (self.root / f"{self.HEARTBEATS[side]}-heartbeat.json").read_text(encoding="utf-8"))["session"]
            return {"pid": int(session["pid"]), "start_time": float(session["start_time"])}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def heartbeats(self) -> dict[str, datetime | None]:
        """Last heartbeat of each side; a missing or unreadable file is None."""
        seen: dict[str, datetime | None] = {}
        for side, stem in self.HEARTBEATS.items():
            try:
                at = datetime.fromisoformat(
                    json.loads((self.root / f"{stem}-heartbeat.json").read_text(encoding="utf-8"))["at"])
            except (OSError, ValueError, KeyError, TypeError):
                at = None
            seen[side] = at if at is None or at.tzinfo is not None else None
        return seen
