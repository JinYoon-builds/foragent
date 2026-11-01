"""Acceptance for parent-sync-starve. Locked by Foragent; do not edit.

Run real gate ticks against listing facts and a tracker with simulated latency.
Each external per-issue or board call takes four seconds; nested implementation
calls inside FakeTracker are one external operation. No wall-clock sleeps.
"""

from dataclasses import replace
from datetime import timedelta
from math import isfinite

from foragent import gh
from foragent.scheduler import tick
from foragent.state import StateStore
from foragent.tracker import TrackerError
from test_orch_review_handoff import tick_context
from test_parent_follows_children import (
    add_parent, gate_tick, offline_only, parent_case,  # noqa: F401
)
from test_skills_autosync import isolated_home  # noqa: F401


PARENTS = tuple(range(2000, 2041))
FINISHED = PARENTS[-3:]
MAX_TICKS = 6
CALL_SECONDS = 4.0
PER_ISSUE = (
    "get_issue", "sub_issues", "parent_of", "parent_issue", "blocked_by",
    "label_events", "list_comments", "close_issue", "comment", "add_labels",
    "remove_labels", "update_issue", "graphql",
)


class Latency:
    """Instrument the injected transport, leaving scheduler budgets unchanged."""

    def __init__(self, tracker, monkeypatch):
        self.seconds = 0.0
        self.calls = []
        self.depth = 0
        self.rate_limited = None
        monkeypatch.setattr(gh.time, "monotonic", lambda: self.seconds)
        for name in PER_ISSUE:
            original = getattr(tracker, name)

            def external(*args, _name=name, _original=original, **kwargs):
                if self.depth:
                    return _original(*args, **kwargs)
                self.depth += 1
                try:
                    try:
                        available = gh.call_timeout(float("inf"))
                    except gh.DeadlineExhausted as exc:
                        self.calls.append((_name, args[0], True, 0.0))
                        raise TrackerError(str(exc)) from exc
                    if isfinite(available):
                        assert available <= 5.0, "Keep the existing five-second gh call cap"
                    spent = min(CALL_SECONDS, available)
                    self.calls.append((_name, args[0], isfinite(available), spent))
                    self.seconds += spent
                    if available < CALL_SECONDS:
                        raise TrackerError("fake gh call timed out at its deadline")
                    if _name == "close_issue" and args[0] == self.rate_limited:
                        raise TrackerError("HTTP 429: API rate limit exceeded")
                    return _original(*args, **kwargs)
                finally:
                    self.depth -= 1

            monkeypatch.setattr(tracker, name, external)

    def gate_tick(self, case, *, require_bounded_closes=False):
        self.calls.clear()
        case.clock[0] += timedelta(minutes=5, seconds=1)
        report = tick(tick_context(case, start_runs=False))
        bounded = [call for call in self.calls if call[2]]
        assert sum(call[3] for call in bounded) <= 10.0, (
            f"Parent work exceeded the existing shared ten-second budget: {bounded}"
        )
        assert sum(call[3] == CALL_SECONDS for call in bounded) <= 2, (
            "The transport must permit only a few calls per tick"
        )
        if require_bounded_closes:
            assert all(bound for name, number, bound, _ in self.calls
                       if name == "close_issue" and number in PARENTS), (
                "Routine parent closes must stay inside the existing optional budget"
            )
        return report


def population(case, monkeypatch):
    """The listing carries native summary facts, including child-only changes."""
    case.config = case.ctx.config = replace(case.config, language="en")
    edges = {number: add_parent(case, ["unapproved", "unapproved"], number=number)
             for number in PARENTS}
    original = case.tracker.list_issues

    def listing(state="all"):
        issues = []
        for issue in original(state):
            if issue.number in edges:
                children = [case.tracker.issues[n] for n in edges[issue.number]]
                ended = sum(child.state == "closed" for child in children)
                issue = replace(issue, sub_issues_summary=(
                    ("total", len(children)), ("completed", ended),
                    ("percent_completed", 100 * ended // len(children)),
                ))
            issues.append(issue)
        return issues

    monkeypatch.setattr(case.tracker, "list_issues", listing)
    # Establish observations and board columns before measuring unchanged work.
    gate_tick(case)
    gate_tick(case, refresh=True)
    return edges


def finish_tail(case, edges):
    # Parent timestamps and bodies stay unchanged: child facts and the listed
    # summary must be enough to notice completion, including cancellation.
    for number, reasons in zip(FINISHED, (
        ("completed", "completed"),
        ("completed", "not_planned"),
        ("not_planned", "not_planned"),
    ), strict=True):
        for child, reason in zip(edges[number], reasons, strict=True):
            case.tracker.issues[child] = replace(
                case.tracker.issues[child], state="closed", state_reason=reason,
            )


def assert_closed_once(case, edges):
    for number in FINISHED:
        assert case.tracker.issues[number].state == "closed", (
            f"Finished parent #{number} starved behind unchanged lower numbers; "
            f"all three must close within {MAX_TICKS} ticks"
        )
        assert sum(n == number for n, _ in case.tracker.closures) == 1
        comments = case.tracker.comments.get(number, [])
        assert len(comments) == 1, f"Parent #{number} must have one closing summary"
        assert all(f"#{child}" in comments[0].body for child in edges[number])
    assert all(case.tracker.issues[n].state == "open" for n in PARENTS[:-3]), (
        "Unfinished parents must remain open"
    )


def test_finished_tail_closes_within_six_ticks_and_stays_closed(parent_case, monkeypatch):
    case = parent_case
    edges = population(case, monkeypatch)
    finish_tail(case, edges)
    latency = Latency(case.tracker, monkeypatch)
    for _ in range(MAX_TICKS):
        assert latency.gate_tick(case, require_bounded_closes=True).skipped is None
    assert_closed_once(case, edges)

    closures = list(case.tracker.closures)
    comments = {n: list(case.tracker.comments[n]) for n in FINISHED}
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    for _ in range(3):
        assert latency.gate_tick(case).skipped is None
        assert latency.calls == [], "Settled parents must cost no per-issue or board calls"
    assert case.tracker.closures == closures
    assert {n: case.tracker.comments[n] for n in FINISHED} == comments


def test_unchanged_parents_need_no_per_issue_or_board_calls(parent_case, monkeypatch):
    case = parent_case
    population(case, monkeypatch)
    latency = Latency(case.tracker, monkeypatch)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    for _ in range(3):
        assert latency.gate_tick(case).skipped is None
        assert latency.calls == [], (
            f"A fresh unchanged listing must need no parent calls: {latency.calls}"
        )


def test_tail_rate_limit_pauses_the_tick_then_recovers_once(parent_case, monkeypatch):
    case = parent_case
    edges = population(case, monkeypatch)
    finish_tail(case, edges)
    latency = Latency(case.tracker, monkeypatch)
    latency.rate_limited = FINISHED[0]
    report = latency.gate_tick(case)
    assert report.skipped and "GitHub rate limit" in report.skipped, (
        "A high-number parent's rate limit must pause the tick, not hide behind starvation"
    )
    assert case.store.github_wait_until(case.clock[0]) is not None
    assert any(name == "close_issue" and number == FINISHED[0]
               for name, number, _, _ in latency.calls)

    latency.calls.clear()
    assert "GitHub rate limit" in tick(tick_context(case, start_runs=False)).skipped
    assert latency.calls == [], "A paused tick must not keep calling GitHub"

    until = case.store.github_wait_until(case.clock[0])
    case.clock[0] = until + timedelta(seconds=1)
    latency.rate_limited = None
    for _ in range(MAX_TICKS):
        assert latency.gate_tick(case).skipped is None
    assert_closed_once(case, edges)
