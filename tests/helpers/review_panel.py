"""Shared offline helpers for unit tests."""

from dataclasses import asdict, dataclass, field
from importlib import import_module
import socket
from threading import Barrier, BrokenBarrierError, Lock
from types import SimpleNamespace

import pytest

from foragent.config import Config
from foragent.executors import ExecResult

from helpers.review_case import answers_scores
from helpers.risk import entry, offline, risk_case


class PanelExecutor:
    """Observe real executor overlap; never implement scheduling or merging.

    The first N calls are independent reviews, the next call is the merger.
    No prompt keywords or unspecified merger role/option names are required.
    A broken barrier returns a normal fake result so the test can report the
    missing overlap at the test boundary, even if the runner catches exceptions.
    """

    def __init__(self, messages, merged="VERDICT: PASS", *, failed=None, timed_out=False, overlap=1):
        self.messages = messages
        self.merged = merged
        self.failed = failed
        self.timed_out = timed_out
        self.barrier = Barrier(min(overlap, len(messages)))
        self.active = self.maximum = 0
        self.lock = Lock()
        self.calls = []
        self.reviews = []
        self.merges = []
        self.completed = set()
        self.overlap_errors = []
        self.early_merge = False

    def run(self, **kwargs):
        with self.lock:
            index = len(self.calls)
            self.calls.append(kwargs)
            if index < len(self.messages):
                self.reviews.append(kwargs)
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            else:
                self.merges.append(kwargs)
                self.early_merge |= len(self.completed) != len(self.messages)
        if index >= len(self.messages):
            return ExecResult(0, False, self.merged, kwargs["log_path"])
        if index < len(self.messages) - len(self.messages) % self.barrier.parties:
            try:
                self.barrier.wait(timeout=5.0)
            except BrokenBarrierError:
                with self.lock:
                    self.overlap_errors.append(index)
        with self.lock:
            self.active -= 1
            self.completed.add(index)
        failed = index == self.failed
        # Timeout is an executor-result flag, with no process or signal path.
        # Keep exit_code=0 in that case to exercise timeout handling itself.
        return ExecResult(1 if failed and not self.timed_out else 0, failed and self.timed_out,
                          self.messages[index], kwargs["log_path"])


def install_panel(case, panel, *, implement=True):
    def execute(**kwargs):
        if implement and not case.implementer.calls:
            return case.implementer.run(**kwargs)
        return panel.run(**kwargs)

    # The contract does not choose a model for the merge step. Capture calls to
    # either executor after the one ordinary implementation call.
    executor = SimpleNamespace(run=answers_scores(execute))
    case.ctx.executor_factory = lambda name: executor
    # #1169 (2026-10-31, test owner): every reviewer counts in the shared machine budget,
    # so the panel gets a budget large enough for the concurrency these cases observe.
    from datetime import UTC, datetime

    from foragent import capacity
    capacity.write_state(case.store, 9, datetime.now(UTC))


_CHECKLIST = [
    {"name": item["name"], "focus": f"FOCUS-{item['name']}: distinct project focus"}
    for item in import_module("foragent.review_perspectives").DEFAULT_PERSPECTIVES
]


@pytest.fixture(autouse=True)
def review_offline(monkeypatch, offline):
    from foragent.executors import ClaudeExecutor, CodexExecutor

    def forbidden(*args, **kwargs):
        pytest.fail("Perspective acceptance must use fake executors and no network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(CodexExecutor, "run", forbidden)


@dataclass(frozen=True)
class _ReviewConfig(Config):
    # YAML validation is tested separately. Supply the proposed public fields to
    # behavioral tests so today's failures expose missing review behavior rather
    # than all stopping at Config's unknown-key/type error. Retain a dataclass so
    # the runner's ordinary lane_config/replace path still runs unchanged.
    review_perspectives: list = field(default_factory=list)
    review_reviewers: int = 1


def perspective_case(git_history, tmp_path, monkeypatch, *, count=1,
                     risk_paths=("feature*.txt",), extra=(), **settings):
    case = risk_case(git_history, tmp_path, monkeypatch,
                     entries=[entry(list(risk_paths))], **settings)
    case.config = case.ctx.config = _ReviewConfig(**{
        **asdict(case.config), "review_reviewers": count,
        "review_perspectives": [dict(item) for item in (*_CHECKLIST, *extra)],
    })
    return case
