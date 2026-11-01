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
from foragent.state_flow import FlowRecordsMixin
from foragent.state_issues import (  # noqa: F401
    REFRESH_INTERVAL,
    IssueSnapshotMixin,
    _forget_reads,
    observed_issue_facts,
)
from foragent.state_local import LocalRecordsMixin
from foragent.state_observations import IssueObservationsMixin
from foragent.state_locks import _TICK_LOCKS_HELD, StateLocksMixin, _held_tick_locks  # noqa: F401
from foragent.state_records import Approval, Flag, InspectState, Run  # noqa: F401
from foragent.state_refresh import IssueRefreshMixin
from foragent.state_github import GithubCallsMixin
from foragent.state_logs import LogRetentionMixin
from foragent.state_runs import RunRecordsMixin
from foragent.state_talk import TalkRecordsMixin

# The <state>/logs/ size cap; prune_logs deletes the oldest issue logs above it.
LOG_LIMIT_BYTES = 500 * 1024 * 1024


class StateStore(
    IssueSnapshotMixin, IssueObservationsMixin, IssueRefreshMixin, RunRecordsMixin, TalkRecordsMixin, GithubCallsMixin, LocalRecordsMixin,
    FlowRecordsMixin, LogRetentionMixin, StateLocksMixin, StateBase,
):
    @classmethod
    def for_repo(cls, repo: str, *, now: Callable[[], datetime] | None = None) -> "StateStore":
        return cls(Path.home() / ".local/share/foragent" / repo.replace("/", "__"), now)

    # Heartbeat file stem for each side; each write overwrites one small file.
    HEARTBEATS = {"orchestrator": "orch", "gate": "gate"}

    # The orchestrator heartbeat also names its active Claude session (#413); writes keep these.
    SESSION_FIELDS = ("session_id", "transcript_path", "started_at", "status", "previous")

    def write_heartbeat(self, side: str, at: datetime, *, session: dict | None = None,
                        session_id: str | None = None) -> None:
        """Overwrite one side's heartbeat with ``at``, keeping the orchestrator's session fields.

        ``session`` is the writer's Claude session ``{pid, start_time}``, if known.
        ``session_id`` is the writer's ``FORAGENT_SESSION_ID``: a write from another
        session than the active one (a retired session) is ignored; a write without
        an id is accepted. The orchestrator side reads and replaces under the state
        lock, so it never undoes a refresh that switched the active session.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{self.HEARTBEATS[side]}-heartbeat.json"
        record: dict = {"at": at.isoformat()}
        if session is not None:
            record["session"] = session
        if side != "orchestrator":
            self._write_json(path, record)
            return
        with self._state_lock():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old = {}
            old = old if isinstance(old, dict) else {}
            active = old.get("session_id")
            if session_id and active and session_id != active:
                return
            record.update({key: old[key] for key in self.SESSION_FIELDS if key in old})
            self._write_json(path, record)

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
