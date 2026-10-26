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
from foragent.state_runs import RunRecordsMixin


class StateStore(
    IssueSnapshotMixin, IssueRefreshMixin, RunRecordsMixin, LocalRecordsMixin, StateLocksMixin, StateBase,
):
    @classmethod
    def for_repo(cls, repo: str, *, now: Callable[[], datetime] | None = None) -> "StateStore":
        return cls(Path.home() / ".local/share/foragent" / repo.replace("/", "__"), now)
