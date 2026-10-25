"""Acceptance test for parent-follows-children. Locked by Foragent; do not edit."""

from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import json
import re
import socket

import pytest

from foragent import board
from foragent.scheduler import tick
from foragent.state import StateStore
from foragent.tracker import FakeTracker, Issue
from test_board_projects import board_snapshot
from test_board_status_groups import card_column, column_updates, record_board_gh, seed_card
from test_local_view import record_gh
from test_orch_review_handoff import make_case, tick_context
from test_skills_autosync import isolated_home


BACKLOG = "🗂 Backlog"
UP_NEXT = "🌰 Up next"
IN_PROGRESS = "🐿 In progress"
YOUR_TURN = "✋ Your turn"
DONE = "📦 Done"
NOT_PLANNED_FLAG = "parent-child-not-planned"


@dataclass(frozen=True)
class ClosureIssue(Issue):
    """Expose GitHub's state_reason through get_issue/list_issues/sub_issues.

    Main's Issue omits this fact. This test-only extension supplies the public
    Tracker result without implementing any parent aggregation or gate behavior.
    """

    state_reason: str | None = None


class ClosureTracker(FakeTracker):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closures = []

    def add_issue(self, *args, state_reason=None, **kwargs):
        issue = super().add_issue(*args, **kwargs)
        issue = ClosureIssue(**{**asdict(issue), "state_reason": state_reason})
        self.issues[issue.number] = issue
        return issue

    def close_issue(self, n, comment=None):
        # Like gh issue close, the public default closes as completed.
        super().close_issue(n, comment)
        self.issues[n] = replace(self.get_issue(n), state_reason="completed")
        self.closures.append((n, "completed"))


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, isolated_home):
    from foragent import executors, notify, runner

    def forbidden(*args, **kwargs):
        pytest.fail("Parent aggregation must use facts without network, models or notifications")

    calls = record_gh(monkeypatch)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(executors, "executor_for", forbidden)
    monkeypatch.setattr(runner, "executor_for", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(notify, "send", forbidden)
    yield
    assert calls == [], "All GitHub operations must stay at the injected offline boundary"


@pytest.fixture
def parent_case(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch,
                     skill_libraries=[], test_command="true")
    case.work_body = case.tracker.get_issue(1).body
    case.store.finish_run(1, outcome="cancelled")
    case.tracker = ClosureTracker(now=lambda: case.clock[0], strict=True)
    case.ctx.tracker = case.tracker
    case.tracker.add_issue(1, "Unused fixture work", state="closed", state_reason="completed")
    case.board_id = board.ensure(case.tracker, "owner/repo")
    assert case.board_id, "The offline board must be configured before testing parent behavior"
    case.config = case.ctx.config = replace(case.config, board=case.board_id)

    def no_turn(**kwargs):
        pytest.fail("A parent aggregation tick must not run an implementer or reviewer")

    monkeypatch.setattr(case.implementer, "run", no_turn)
    monkeypatch.setattr(case.reviewer, "run", no_turn)
    return case


def add_parent(case, states, *, number=10, approved_work=False):
    """Seed publish's plan marker and native edges through public tracker methods."""
    children = list(range(number * 10 + 1, number * 10 + 1 + len(states)))
    mapping = {f"child-{index}": child for index, child in enumerate(children)}
    marker = (f"<!-- foragent:plan id=parent-{number} map={json.dumps(mapping)} "
              "acceptance={} -->")
    body = (case.work_body if approved_work else "Ship the order.") + "\n\n" + marker
    case.tracker.add_issue(number, f"Order {number}", body)
    if approved_work:
        case.tracker.record_label(number, "approved", "human")
    board.add_issue(case.tracker, "owner/repo", case.board_id, number)
    labels = {"unapproved": [], "approved": ["approved"], "ready": ["approved", "ready"],
              "running": ["approved", "running"], "pr-open": ["approved"],
              "blocked": ["blocked"], "needs-human": ["needs-human"],
              "completed": [], "not_planned": []}
    for child, state in zip(children, states, strict=True):
        closed = state in {"completed", "not_planned"}
        case.tracker.add_issue(child, f"Child {child}", case.work_body,
                               labels=labels[state], state="closed" if closed else "open",
                               state_reason=state if closed else None)
        case.tracker.add_sub_issue(number, child)
        if "approved" in labels[state]:
            case.tracker.record_label(child, "approved", "human")
        if state == "running":
            case.store.start_run(child, branch=f"foragent/issue-{child}",
                                 worktree=case.store.worktree_path(child))
        if state == "pr-open":
            case.tracker.create_pr(head=f"foragent/issue-{child}", base="main",
                                   title=f"Child {child}", body=f"Closes #{child}")
    return children


def gate_tick(case, *, refresh=False):
    if refresh:
        case.clock[0] += timedelta(minutes=5, seconds=1)
    report = tick(tick_context(case, start_runs=False))
    assert report.skipped is None, f"An offline parent tick must run: {report}"


@pytest.mark.parametrize(("states", "expected"), [
    pytest.param(["needs-human", "unapproved"], YOUR_TURN, id="human-over-unapproved"),
    pytest.param(["completed", "running", "blocked", "needs-human"], YOUR_TURN,
                 id="human-over-progress-and-done"),
    pytest.param(["running", "unapproved"], IN_PROGRESS, id="running"),
    pytest.param(["pr-open", "unapproved"], IN_PROGRESS, id="pr-open"),
    pytest.param(["blocked", "unapproved"], IN_PROGRESS, id="blocked"),
    pytest.param(["completed"] * 3 + ["unapproved"] * 2, IN_PROGRESS, id="sixty-percent-done"),
    pytest.param(["completed", "approved"], IN_PROGRESS, id="done-over-up-next"),
    pytest.param(["approved", "approved"], UP_NEXT, id="all-approved"),
    pytest.param(["approved", "ready"], UP_NEXT, id="approved-and-ready"),
])
def test_parent_column_follows_children(parent_case, states, expected):
    case = parent_case
    add_parent(case, states)
    gate_tick(case)
    actual = card_column(case.tracker, case.board_id, 10)
    assert actual == expected, f"Parent with children {states} must be {expected}; got {actual}"
    assert case.tracker.get_issue(10).state == "open"
    assert "approved" not in case.tracker.get_issue(10).labels, "Aggregation must not approve parents"


def test_parent_returns_to_backlog_when_an_open_child_loses_approval(parent_case):
    case = parent_case
    children = add_parent(case, ["approved", "ready"])
    gate_tick(case)
    assert card_column(case.tracker, case.board_id, 10) == UP_NEXT, (
        "An unstarted parent whose children are approved must leave Backlog"
    )
    case.tracker.remove_labels(children[0], ["approved", "ready"])
    gate_tick(case, refresh=True)
    assert card_column(case.tracker, case.board_id, 10) == BACKLOG, (
        "One unapproved open child returns an unstarted parent to Backlog"
    )
    case.tracker.remove_labels(children[1], ["approved", "ready"])
    gate_tick(case, refresh=True)
    assert card_column(case.tracker, case.board_id, 10) == BACKLOG
    assert case.tracker.get_issue(10).state == "open"


@pytest.mark.parametrize("count", [1, 5])
def test_completed_children_close_parent_once_with_one_line_count(parent_case, count):
    case = parent_case
    children = add_parent(case, ["completed"] * count)
    add_parent(case, [], number=30)  # Empty work must not satisfy a vacuous all-completed check.
    assert len(case.tracker.sub_issues(10)) == len(children) == count
    gate_tick(case)
    parent = case.tracker.get_issue(10)
    assert parent.state == "closed", f"Gate must close parent after all {count} children completed"
    assert parent.state_reason == "completed", "Parent must close as completed"
    assert card_column(case.tracker, case.board_id, 10) == DONE
    comments = case.tracker.list_comments(10)
    assert len(comments) == 1, "Completing a parent must leave exactly one summary comment"
    assert len(comments[0].body.splitlines()) == 1, "Completion summary must be one line"
    assert re.search(rf"\b{count}\b", comments[0].body), "Completion summary must name the child count"
    assert "approved" not in parent.labels
    assert case.tracker.get_issue(30).state == "open"
    assert card_column(case.tracker, case.board_id, 30) == BACKLOG

    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    for _ in range(2):
        gate_tick(case, refresh=True)
    assert [entry for entry in case.tracker.closures if entry[0] == 10] == [(10, "completed")], (
        "A reopened gate context must not close an already completed parent again"
    )
    assert case.tracker.list_comments(10) == comments
    assert card_column(case.tracker, case.board_id, 10) == DONE


@pytest.mark.parametrize("states", [
    pytest.param(["completed", "not_planned"], id="otherwise-all-complete"),
    pytest.param(["not_planned", "unapproved"], id="open-sibling"),
    pytest.param(["not_planned", "not_planned"], id="two-cancelled-children"),
])
def test_not_planned_child_keeps_parent_open_and_records_one_flag(parent_case, states):
    case = parent_case
    children = add_parent(case, states)
    assert [child.state_reason for child in case.tracker.sub_issues(10)] == [
        state if state in {"completed", "not_planned"} else None for state in states
    ], "The gate must be able to observe child closure reasons through Tracker.sub_issues"
    gate_tick(case)
    assert case.tracker.get_issue(10).state == "open", "A not_planned child must prevent parent completion"
    flag = case.store.load_flag(10)
    assert flag is not None and flag.reason == NOT_PLANNED_FLAG, (
        "A not_planned child must record parent-child-not-planned for the orchestrator"
    )
    comments = case.tracker.list_comments(10)
    assert not any(number == 10 for number, reason in case.tracker.closures)
    assert all(case.tracker.parent_of(child) == 10 for child in children)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    for _ in range(3):
        gate_tick(case, refresh=True)
        assert case.store.load_flag(10) == flag, "Unchanged cancellations must not renew the flag"
        assert case.tracker.list_comments(10) == comments, "Unchanged cancellations must not repeat comments"
        assert case.tracker.get_issue(10).state == "open"
        assert card_column(case.tracker, case.board_id, 10) != DONE


@pytest.mark.parametrize("states", [
    pytest.param(["completed", "completed"], id="completed-children"),
    pytest.param(["needs-human", "unapproved"], id="human-child"),
    pytest.param(["not_planned", "completed"], id="cancelled-child"),
])
def test_approved_issue_with_children_keeps_its_own_work_state(parent_case, states):
    """Pair a work control with an order so every collected case exposes main's bug."""
    case = parent_case
    add_parent(case, states)
    add_parent(case, states, number=20, approved_work=True)
    gate_tick(case)
    work = case.tracker.get_issue(20)
    assert work.state == "open", "Completed children do not complete approved work itself"
    assert work.labels == {"approved", "ready"}, "Approved parents must use the ordinary work gate"
    assert card_column(case.tracker, case.board_id, 20) == UP_NEXT, (
        "Approved work must keep its own column despite child human/progress/done states"
    )
    assert case.store.load_flag(20) is None, "Child cancellations must not flag approved work"
    assert not any(number == 20 for number, reason in case.tracker.closures)
    if "not_planned" in states:
        flag = case.store.load_flag(10)
        assert flag is not None and flag.reason == NOT_PLANNED_FLAG, (
            "The equivalent unapproved order must flag its cancelled child"
        )
    elif "needs-human" in states:
        assert card_column(case.tracker, case.board_id, 10) == YOUR_TURN, (
            "The equivalent unapproved order must follow its human child"
        )
    else:
        assert case.tracker.get_issue(10).state == "closed", (
            "The equivalent unapproved order must close after its children complete"
        )


def test_parent_board_mutates_only_when_the_computed_column_changes(parent_case, monkeypatch):
    case = parent_case
    child, sibling = add_parent(case, ["blocked", "unapproved"])
    seed_card(case.tracker, case.board_id, 10, IN_PROGRESS)
    item_id = next(item["id"] for item in board_snapshot(case.tracker, case.board_id)["items"]["nodes"]
                   if item["content"]["number"] == 10)
    calls = record_board_gh(case.tracker, monkeypatch)
    observed = []

    def measure(*, refresh=False):
        calls.clear()
        gate_tick(case, refresh=refresh)
        writes = [call for call in column_updates(calls) if call["variables"]["input"]["itemId"] == item_id]
        observed.append((card_column(case.tracker, case.board_id, 10), len(writes)))
        for _ in range(2):
            calls.clear()
            gate_tick(case)
            assert not [call for call in calls if call["query"].lstrip().startswith("mutation")], (
                "An unchanged tick must make zero board mutations, including redundant card additions"
            )
            assert calls == [], "An unchanged tick must reuse recorded board values without a board call"

    measure()  # The seeded progress column is already correct: no parent write.
    case.tracker.remove_labels(child, ["blocked"])
    case.tracker.add_labels(child, ["needs-human"])
    measure(refresh=True)
    case.tracker.remove_labels(child, ["needs-human"])
    case.tracker.add_labels(child, ["blocked"])
    measure(refresh=True)
    case.tracker.remove_labels(child, ["blocked"])
    case.tracker.close_issue(child)  # Partial completion still belongs in progress.
    measure(refresh=True)
    case.tracker.close_issue(sibling)
    measure(refresh=True)

    assert observed == [(IN_PROGRESS, 0), (YOUR_TURN, 1), (IN_PROGRESS, 1),
                        (IN_PROGRESS, 0), (DONE, 1)], (
        "Parent Status must update once per computed column change and never for a same-column change; "
        f"got {observed}"
    )
    assert case.tracker.get_issue(10).state == "closed"
