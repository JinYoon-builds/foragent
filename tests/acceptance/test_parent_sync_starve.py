"""Acceptance for parent-sync-starve. Locked by Foragent; do not edit.

Run real gate ticks against listing facts and a tracker with simulated latency.
Each external per-issue or board call takes four seconds; nested implementation
calls inside FakeTracker are one external operation. No wall-clock sleeps.
"""

from dataclasses import replace
from datetime import timedelta
from math import isfinite

import pytest

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


class SequenceLatency(Latency):
    """Observe completed external calls, without replacing any parent behavior."""

    def __init__(self, tracker, monkeypatch):
        super().__init__(tracker, monkeypatch)
        self.completed = []
        self.listing = None
        listing = tracker.list_issues

        def observe_listing(*args, **kwargs):
            issues = listing(*args, **kwargs)
            self.listing = tuple(sorted(issues, key=lambda issue: issue.number))
            return issues

        monkeypatch.setattr(tracker, "list_issues", observe_listing)
        for name in PER_ISSUE:
            original = getattr(tracker, name)

            def observe(*args, _name=name, _original=original, **kwargs):
                outer = not self.depth
                start = len(self.calls)
                result = _original(*args, **kwargs)
                if outer and self.calls[start][2]:
                    key = (_name, repr(args), repr(sorted(kwargs.items())))
                    read = (_name in {"get_issue", "sub_issues", "parent_of", "parent_issue",
                                      "blocked_by", "label_events", "list_comments"}
                            or _name == "graphql" and args[0].lstrip().startswith("query"))
                    self.completed.append((key, read))
                return result

            monkeypatch.setattr(tracker, name, observe)

    def gate_tick(self, case, **kwargs):
        self.completed.clear()
        report = super().gate_tick(case, **kwargs)
        assert all(bounded for name, number, bounded, _ in self.calls
                   if name == "close_issue" and number == 10), (
            "A longer parent action must not escape the shared optional budget"
        )
        return report


def sequence_case(case, monkeypatch, states, references, board_mode, *, race=False):
    """Start from observed open work, then expose a stable actionable listing."""
    from foragent import board
    from test_parent_follows_children import IN_PROGRESS, seed_card

    case.config = case.ctx.config = replace(case.config, language="en")
    children = add_parent(case, ["unapproved"] * len(states))
    extra = list(range(9001, 9001 + references))
    for number in extra:
        case.tracker.add_issue(number, f"Referenced work {number}")
    mapping = ", ".join(f'"work-{n}": {n}' for n in children + extra)
    parent = case.tracker.issues[10]
    case.tracker.issues[10] = replace(
        parent, body='Ship the order.\n\n<!-- foragent:plan id=sequence map={'
        + mapping + '} acceptance={} -->',
    )
    listing = case.tracker.list_issues
    hidden = set(extra)

    def listed(state="all"):
        issues = []
        for issue in listing(state):
            if issue.number in hidden:
                continue
            if issue.number == 10:
                ended = sum(case.tracker.issues[n].state == "closed" for n in children)
                issue = replace(issue, sub_issues_summary=(
                    ("total", len(children)), ("completed", ended),
                    ("percent_completed", 100 * ended // len(children) if children else 0),
                ))
            issues.append(issue)
        return issues

    monkeypatch.setattr(case.tracker, "list_issues", listed)
    closed_since = case.tracker.issues_closed_since
    monkeypatch.setattr(case.tracker, "issues_closed_since", lambda since: [
        issue for issue in closed_since(since) if issue.number not in hidden
    ])  # Unlisted references must also be absent from the recent-close listing.
    gate_tick(case)
    gate_tick(case, refresh=True)

    # Change public GitHub facts, not the gate's stored progress or read cache.
    for number, state in zip(children, states, strict=True):
        ended = state in {"completed", "not_planned"}
        case.tracker.issues[number] = replace(
            case.tracker.issues[number], state="closed" if ended else "open",
            state_reason=state if ended else None,
            labels=frozenset({"needs-human"} if state == "needs-human" else ()),
        )
    for number in extra:
        case.tracker.issues[number] = replace(
            case.tracker.issues[number], state="closed", state_reason="completed",
        )
    # Also exercise invalidation by a new parent listing, including cases whose
    # native completion count did not change.
    case.tracker.issues[10] = replace(
        case.tracker.issues[10], updated_at=(case.clock[0] + timedelta(seconds=1)).isoformat(),
    )

    if race:
        assert len(children) == 2
        hidden.add(children[-1])
        sub_issues = case.tracker.sub_issues
        first = True

        def raced_edges(number):
            nonlocal first
            result = sub_issues(number)
            if number == 10 and first:
                first = False
                return result[:1]  # The listing says two; this edge response still says one.
            return result

        monkeypatch.setattr(case.tracker, "sub_issues", raced_edges)

    # The existing offline fake has no board/field/card deletion API. These are
    # fixture changes to GitHub's board, never edits to the gate's own state.
    project = case.tracker._projects.projects[case.board_id]
    status = next(field for field in project["fields"].values() if field["name"] == "Status")
    if board_mode == "no-board":
        case.config = case.ctx.config = replace(case.config, board=None)
    elif board_mode == "missing-done":
        status["options"] = [option for option in status["options"] if option["name"] != board.COLUMNS[-1]]
    elif board_mode == "missing-status":
        del project["fields"][status["id"]]
        for item in project["items"].values():
            item["values"].pop(status["id"], None)
    elif board_mode == "deleted-board":
        del case.tracker._projects.projects[case.board_id]
    elif board_mode == "missing-card":
        project["items"] = {key: item for key, item in project["items"].items()
                            if item["contentId"] != "I_owner/repo#10"}
    elif board_mode == "return-to-backlog":
        seed_card(case.tracker, case.board_id, 10, IN_PROGRESS)
    else:
        assert board_mode == "available"
    return children, extra


def finish_sequence(case, latency, sequence, *, closes, column=None):
    """One rule for every path: retain unchanged reads and make progress each tick.

    The table describes the work, not a required implementation call order.
    Deriving a result from existing listing facts may eliminate calls entirely.
    """
    from test_parent_follows_children import card_column

    previous_listing = None
    reads, successes = set(), set()
    limit = len(sequence) + 3  # Allow discovery reads as well as the listed work.
    for turn in range(1, limit + 1):
        assert latency.gate_tick(case).skipped is None
        if latency.listing != previous_listing:
            reads.clear()
            successes.clear()
            previous_listing = latency.listing
        completed = {key for key, _ in latency.completed}
        assert completed - successes, (
            f"Tick {turn} restarted {sequence} without completing a new call: {latency.calls}"
        )
        for key, read in latency.completed:
            if read:
                assert key not in reads, (
                    f"Tick {turn} repeated a completed read with an unchanged listing: {key}; "
                    f"remaining work must advance for every sequence, including {sequence}"
                )
                reads.add(key)
        successes.update(completed)
        if closes:
            done = case.tracker.issues[10].state == "closed"
        else:
            assert case.tracker.issues[10].state == "open", "Open work must keep its parent open"
            done = True
        if column is not None:
            done = done and card_column(case.tracker, case.board_id, 10) == column
        if done:
            break
    else:
        pytest.fail(f"Parent action {sequence} did not finish within {limit} refresh ticks")
    assert len([n for n, _ in case.tracker.closures if n == 10]) == int(closes)
    comments = case.tracker.comments.get(10, [])
    assert len(comments) == int(closes), "A completed sequence must leave exactly one closing summary"
    return comments


# All multi-call shapes share the assertion above. Include longer sequences,
# reference-only orders, board moves and card creation as well as the held paths.
@pytest.mark.parametrize(("states", "references", "board_mode", "race", "sequence"), [
    pytest.param(("completed",), 1, "no-board", False,
                 ("children", "reference", "close"), id="children-reference-close"),
    pytest.param((), 3, "no-board", False,
                 ("reference",) * 3 + ("close",), id="three-references-close"),
    pytest.param((), 4, "no-board", False,
                 ("reference",) * 4 + ("close",), id="four-references-close"),
    pytest.param((), 7, "no-board", False,
                 ("reference",) * 7 + ("close",), id="seven-references-close"),
    pytest.param(("completed", "not_planned"), 3, "no-board", False,
                 ("children",) + ("reference",) * 3 + ("close",), id="mixed-children-references-close"),
    pytest.param(("completed",), 1, "available", False,
                 ("children", "reference", "close", "board", "card", "status"),
                 id="native-reference-close-and-done"),
    pytest.param(("completed",), 1, "missing-done", False,
                 ("board", "reference", "close"), id="native-reference-missing-done"),
    pytest.param(("completed",), 1, "missing-status", False,
                 ("board", "reference", "close"), id="native-reference-missing-status"),
    pytest.param(("completed",), 1, "deleted-board", False,
                 ("board", "reference", "close"), id="native-reference-deleted-board"),
    pytest.param(("completed", "completed"), 1, "no-board", True,
                 ("children", "reference", "close"), id="edge-race-reference-close"),
    pytest.param(("completed", "unapproved"), 0, "available", True,
                 ("children", "board", "card", "status"), id="edge-race-board-status"),
    pytest.param(("completed", "unapproved"), 0, "available", False,
                 ("children", "board", "card", "status"), id="children-board-status"),
    pytest.param(("completed", "unapproved"), 0, "missing-card", False,
                 ("children", "board", "card", "add-card", "status"), id="children-add-card-status"),
    pytest.param(("needs-human", "unapproved"), 0, "available", False,
                 ("children", "board", "card", "status"), id="children-human-status"),
    pytest.param(("unapproved", "unapproved"), 0, "return-to-backlog", False,
                 ("children", "board", "card", "status"), id="children-backlog-status"),
])
def test_every_parent_call_sequence_advances(
    parent_case, monkeypatch, states, references, board_mode, race, sequence,
):
    from test_parent_follows_children import BACKLOG, DONE, IN_PROGRESS, YOUR_TURN

    case = parent_case
    children, extra = sequence_case(case, monkeypatch, states, references, board_mode, race=race)
    latency = SequenceLatency(case.tracker, monkeypatch)
    closes = all(state in {"completed", "not_planned"} for state in states)
    column = None
    if board_mode in {"available", "missing-card", "return-to-backlog"}:
        column = (DONE if closes else YOUR_TURN if "needs-human" in states
                  else IN_PROGRESS if "completed" in states else BACKLOG)
    comments = finish_sequence(case, latency, sequence, closes=closes, column=column)
    if closes:
        assert all(f"#{n}" in comments[0].body for n in children + extra)


def test_listing_change_invalidates_completed_parent_reads(parent_case, monkeypatch):
    case = parent_case
    children, extra = sequence_case(case, monkeypatch, ("completed",), 3, "no-board")
    latency = SequenceLatency(case.tracker, monkeypatch)
    assert latency.gate_tick(case).skipped is None
    assert case.tracker.issues[10].state == "open", "Three reference reads and a close cannot fit in one tick"
    assert any(key[0] == "get_issue" and key[1] == repr((extra[0],))
               for key, _ in latency.completed), "First establish a completed reference read"

    # Reopen exactly the reference already read as ended, and advance the parent
    # listing. Reusing that earlier result would close an order with open work.
    case.tracker.issues[extra[0]] = replace(case.tracker.issues[extra[0]], state="open", state_reason=None)
    case.tracker.issues[10] = replace(case.tracker.issues[10], updated_at=case.clock[0].isoformat())
    fresh = False
    for _ in range(len(extra) + 2):
        assert latency.gate_tick(case).skipped is None
        fresh |= any(key[0] == "get_issue" and key[1] == repr((extra[0],))
                     for key, _ in latency.completed)
        assert case.tracker.issues[10].state == "open", "A changed listing must invalidate the earlier ended read"
        assert not case.tracker.closures and not case.tracker.comments.get(10)
    assert fresh, "The changed listing must lead to observing the reference's new open state"

    case.tracker.issues[extra[0]] = replace(
        case.tracker.issues[extra[0]], state="closed", state_reason="completed",
    )
    case.tracker.issues[10] = replace(case.tracker.issues[10], updated_at=case.clock[0].isoformat())
    comments = finish_sequence(case, latency, ("children",) + ("reference",) * 3 + ("close",), closes=True)
    assert all(f"#{n}" in comments[0].body for n in children + extra)
