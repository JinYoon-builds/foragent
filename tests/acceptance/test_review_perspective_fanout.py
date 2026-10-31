"""Locked review-direction acceptance: one perspective per whole-change reviewer.

Break-it: rejects one generalist, reviewer-count-sized perspective shares,
path-sized reviews and omitted project perspectives.
Only public runner/executor boundaries are exercised; the fake never schedules
reviewers, groups defects or decides whether a claim is real.
"""

from dataclasses import replace
import re
from threading import Barrier, BrokenBarrierError, Lock
from types import SimpleNamespace

import pytest

from foragent import capacity
from foragent.executors import ExecResult
from foragent.runner import run_issue
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import copy_history, entry, offline, risk_case  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


PROJECT = {"name": "observability", "focus": "PROJECT-TRACE: preserve diagnostic provenance"}
PERSPECTIVE_COUNT = 5  # Four person-selected perspectives plus this project perspective.
MAPPED = "src/runtime/ownership.py"
UNMAPPED = "src/ordinary.py"


def direction_case(git_history, tmp_path, monkeypatch, *, mapped=True, **settings):
    case = risk_case(
        git_history, tmp_path, monkeypatch,
        entries=[entry([MAPPED] if mapped else ["docs/**"])],
        changed_paths=(MAPPED, UNMAPPED),
        **{"skill_libraries": [], "test_command": "true", "review_max": 0,
           "max_retries": 1, **settings},
    )
    # An existing count of one must limit concurrency, not remove perspectives.
    case.config = case.ctx.config = replace(
        case.config, review_perspectives=[PROJECT], review_reviewers=1, language="en",
    )
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    return case


def is_lane_turn(kwargs):
    # Recognize the existing public lane prompt, including native /goal and resume.
    return re.search(r"(?:You are|Continue) implementing GitHub issue #1\b", kwargs["prompt"]) is not None


def install_review(case, review):
    def execute(**kwargs):
        if is_lane_turn(kwargs):
            return case.implementer.run(**kwargs)
        return review(**kwargs)

    # Both models are captured: choosing the merger's model belongs to the lane.
    case.ctx.executor_factory = lambda name: SimpleNamespace(run=execute)


class FirstRound:
    def __init__(self, case, messages, merged, *, overlap=1, accounted=False):
        self.case, self.messages, self.merged = case, messages, merged
        self.accounted = accounted
        self.calls, self.reviews, self.merges = [], [], []
        self.lock = Lock()
        self.pair = Barrier(overlap, timeout=5) if overlap > 1 else None
        self.active = self.maximum = self.completed = 0
        self.early_merge = False
        self.errors = []

    def run(self, **kwargs):
        with self.lock:
            index = len(self.calls)
            self.calls.append(kwargs)
            if index >= len(self.messages):
                self.merges.append(kwargs)
                self.early_merge |= self.completed != len(self.messages) or self.active != 0
                return ExecResult(0, False, self.merged, kwargs["log_path"])
            self.reviews.append(kwargs)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.accounted:
                holders = capacity.machine_runs(self.case.store, self.case.clock[0])
                budget = capacity.workload_budget(self.case.store)
                if not self.active <= holders <= budget:
                    self.errors.append(f"Shared machine accounting: active={self.active}, holders={holders}, budget={budget}")
        try:
            if self.pair is not None and index < len(self.messages) - len(self.messages) % self.pair.parties:
                try:
                    self.pair.wait()
                except BrokenBarrierError:
                    self.errors.append("Available reviewer slots were not used concurrently")
            return ExecResult(0, False, self.messages[index], kwargs["log_path"])
        finally:
            with self.lock:
                self.active -= 1
                self.completed += 1


def assert_first_round(panel):
    assert len(panel.reviews) == PERSPECTIVE_COUNT, (
        f"Round 1 needs {PERSPECTIVE_COUNT} perspective reviewers; got {len(panel.reviews)}"
    )
    assert len(panel.merges) == 1, "Return exactly one authoritative merged result"
    assert not panel.early_merge, "Collect every perspective before returning findings"
    assert len(panel.calls) == PERSPECTIVE_COUNT + 1


def test_risk_round_assigns_each_perspective_the_whole_change(
    git_history, tmp_path, monkeypatch,
):
    # Negative control paired with the positive case: risk-map gating remains real.
    ordinary_history = copy_history(git_history, tmp_path / "ordinary")
    ordinary = direction_case(ordinary_history, tmp_path / "ordinary", monkeypatch, mapped=False)
    calls = []

    def ordinary_review(**kwargs):
        calls.append(kwargs)
        return ExecResult(0, False, "VERDICT: PASS", kwargs["log_path"])

    install_review(ordinary, ordinary_review)
    assert run_issue(1, ordinary.ctx) == "merged" and len(calls) == 1

    case = direction_case(git_history, tmp_path, monkeypatch)
    panel = FirstRound(case, ["VERDICT: PASS"] * PERSPECTIVE_COUNT, "VERDICT: PASS")
    install_review(case, panel.run)
    outcome = run_issue(1, case.ctx)
    assert_first_round(panel)
    # Semantic prompt checks do not prescribe internal seat names or formatting.
    seats = [
        r"goal[ -]+fidelity|fidel\w*.{0,60}goal",
        r"concurren\w*.{0,80}order|order\w*.{0,80}concurren",
        r"interrupt\w*.{0,80}rollback|rollback.{0,80}interrupt",
        r"test[ -]+quality|quality.{0,40}test",
        re.escape(PROJECT["focus"]),
    ]
    assigned = []
    for call in panel.reviews:
        prompt = " ".join(call["prompt"].lower().split())
        matches = [index for index, pattern in enumerate(seats) if re.search(pattern.lower(), prompt)]
        assert len(matches) == 1, "Each reviewer needs one assigned perspective"
        assigned.extend(matches)
        for path in (MAPPED, UNMAPPED, "feature.txt"):
            assert f"diff --git a/{path} b/{path}" in call["prompt"], "Every perspective reads the whole change"
        assert "RISK-MATCH" in call["prompt"]
    assert sorted(assigned) == list(range(PERSPECTIVE_COUNT))
    assert outcome == "merged" and len(case.tracker.merged_prs) == 1
