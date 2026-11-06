"""Shared offline helpers for unit tests."""

from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import json
from math import isfinite
import socket

import pytest

from foragent import board, gh
from foragent.scheduler import tick
from foragent.state import StateStore
from foragent.tracker import FakeTracker, Issue, TrackerError

from helpers.github_transport import record_gh
from helpers.home import isolated_home
from helpers.review_case import make_case, tick_context


def add_record(case, number, *, mapping=None, edited=()):
    acceptance = {str(n): {"tests/acceptance/test_x.py": "0" * 40} for n in edited}
    marker = (f"<!-- foragent:plan id=record-{number} map={json.dumps(mapping or {})} "
              f"acceptance={json.dumps(acceptance)} -->")
    case.tracker.add_issue(number, f"Order {number}", "Answers a question.\n\n" + marker)
    board.add_issue(case.tracker, "owner/repo", case.board_id, number)


def add_work(case, number, *, closed=False):
    case.tracker.add_issue(number, f"Work {number}", case.work_body,
                           labels=[] if closed else ["approved"],
                           state="closed" if closed else "open",
                           state_reason="completed" if closed else None)


def tick_again(case):
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    gate_tick(case, refresh=True)


BACKLOG = "🗂 Backlog"
IN_PROGRESS = "🐿 In progress"
YOUR_TURN = "✋ Your turn"


@dataclass(frozen=True)
class _ClosureIssue(Issue):
    """Expose GitHub's state_reason through get_issue/list_issues/sub_issues.

    Main's Issue omits this fact. This test-only extension supplies the public
    Tracker result without implementing any parent aggregation or gate behavior.
    """

    state_reason: str | None = None


class _ClosureTracker(FakeTracker):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closures = []

    def add_issue(self, *args, state_reason=None, **kwargs):
        issue = super().add_issue(*args, **kwargs)
        issue = _ClosureIssue(**{**asdict(issue), "state_reason": state_reason})
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
    try:  # #129's tick push step is its own feature; these tests pin other tick work.
        from foragent import orch_push
    except ImportError:
        orch_push = None
    if orch_push is not None and hasattr(orch_push, "after_tick"):
        monkeypatch.setattr(orch_push, "after_tick", lambda *args, **kwargs: ([], False))
    yield
    assert calls == [], "All GitHub operations must stay at the injected offline boundary"


@pytest.fixture
def parent_case(git_history, tmp_path, monkeypatch):
    case = make_case(git_history, tmp_path, monkeypatch,
                     skill_libraries=[], test_command="true")
    case.work_body = case.tracker.get_issue(1).body
    case.store.finish_run(1, outcome="cancelled")
    case.tracker = _ClosureTracker(now=lambda: case.clock[0], strict=True)
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


@pytest.fixture(autouse=True)
def offline_github(monkeypatch, isolated_home):
    from helpers.github_transport import record_gh

    calls = record_gh(monkeypatch)
    yield
    assert calls == [], "Board tests must use the injected offline gh runner"


def _status_field(tracker, board_id):
    from foragent.board import Project

    return Project(tracker, "owner/repo", board_id).fields()["Status"]


def seed_card(tracker, board_id, number, column):
    """Seed existing card membership and value at the fake GraphQL boundary."""
    field = _status_field(tracker, board_id)
    option = next(option for option in field["options"] if option["name"] == column)
    item = tracker.graphql("""mutation ForagentAddBoardItem($input: AddProjectV2ItemByIdInput!) {
      addProjectV2ItemById(input: $input) { item { id } }
    }""", {"input": {"projectId": board_id, "contentId": f"I_owner/repo#{number}"}})
    item_id = item["data"]["addProjectV2ItemById"]["item"]["id"]
    tracker.graphql("""mutation ForagentSetBoardValue($input: UpdateProjectV2ItemFieldValueInput!) {
      updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
    }""", {"input": {"projectId": board_id, "itemId": item_id, "fieldId": field["id"],
                     "value": {"singleSelectOptionId": option["id"]}}})


def card_column(tracker, board_id, number):
    project = _board_snapshot(tracker, board_id)
    item = next(item for item in project["items"]["nodes"] if item["content"]["number"] == number)
    return next(value["name"] for value in item["fieldValues"]["nodes"]
                if value["field"]["name"] == "Status")


def _board_snapshot(tracker, board_id):
    """Read observable Projects v2 state through the offline GraphQL transport."""
    result = tracker.graphql(
        """query($id: ID!) {
          node(id: $id) { ... on ProjectV2 {
            id title
            fields(first: 100) { nodes { ... on ProjectV2SingleSelectField {
              name options { name }
            } } }
            items(first: 100) { nodes {
              content { ... on Issue { number } }
              fieldValues(first: 100) { nodes { ... on ProjectV2ItemFieldSingleSelectValue {
                name field { ... on ProjectV2SingleSelectField { name } }
              } } }
            } }
          } }
        }""", {"id": board_id},
    )
    return result.get("data", result)["node"]


_PARENTS = tuple(range(2000, 2041))
_CALL_SECONDS = 4.0
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
                    spent = min(_CALL_SECONDS, available)
                    self.calls.append((_name, args[0], isfinite(available), spent))
                    self.seconds += spent
                    if available < _CALL_SECONDS:
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
        assert sum(call[3] == _CALL_SECONDS for call in bounded) <= 2, (
            "The transport must permit only a few calls per tick"
        )
        if require_bounded_closes:
            assert all(bound for name, number, bound, _ in self.calls
                       if name == "close_issue" and number in _PARENTS), (
                "Routine parent closes must stay inside the existing optional budget"
            )
        return report
