"""Acceptance test for order-republish. Locked by Foragent; do not edit."""

import pytest

from foragent.tracker import TrackerError

from test_order import PublishingTracker, plan, publishing
from test_order_preread import preread, preread_case
from test_publish_out_of_scope import publish_cli
from test_skills_autosync import cli_exit, isolated_home


LINK_ERROR = (
    "Issue may not contain duplicate sub-issues and Sub issue may only have one parent (HTTP 422)"
)


class GitHubLinkTracker(PublishingTracker):
    """Reject duplicate links and competing parents at the native write boundary."""

    def __init__(self, events):
        super().__init__(events)
        self.link_calls = []
        self.sub_issue_reads = []
        self.dependency_calls = []

    def add_sub_issue(self, parent, child):
        self.link_calls.append((parent, child))
        if self.parent_of(child) is not None:
            raise TrackerError(LINK_ERROR)
        super().add_sub_issue(parent, child)

    def sub_issues(self, parent):
        self.sub_issue_reads.append(parent)
        return super().sub_issues(parent)

    def add_blocked_by(self, number, blocker):
        self.dependency_calls.append(("add", number, blocker))
        super().add_blocked_by(number, blocker)

    def remove_blocked_by(self, number, blocker):
        self.dependency_calls.append(("remove", number, blocker))
        super().remove_blocked_by(number, blocker)


@pytest.fixture
def republish_case(preread_case, publishing, monkeypatch):
    case = preread_case
    case.tracker = GitHubLinkTracker(case.events)
    case.main = publish_cli(
        (case.root, case.config, case.tracker, publishing[3], case.events),
        monkeypatch,
    )
    return case


def publish(case, capsys, *options):
    capsys.readouterr()
    code = cli_exit(case.main, ["order", "publish", str(case.path), *options])
    captured = capsys.readouterr()
    assert case.gh == []
    return code, captured.out + captured.err


def published_issues(case):
    issues = {issue.title: issue.number for issue in case.tracker.list_issues()}
    return issues[case.plan["title"]], {
        item["key"]: issues[item["title"]] for item in case.plan["issues"]
    }


def test_order_republish_approves_one_key_after_later_clean_preread(republish_case, capsys):
    case = republish_case
    code, message = publish(case, capsys)
    assert code == 0, message
    parent, children = published_issues(case)
    assert all("approved" not in case.tracker.get_issue(n).labels for n in children.values())
    assert case.tracker.link_calls == [(parent, n) for n in children.values()]

    preread(case, capsys, "NONE", "NONE")
    case.tracker.link_calls.clear()
    case.tracker.sub_issue_reads.clear()
    code, message = publish(case, capsys, "--approve-keys", "client")
    assert code == 0, message
    assert "approved" in case.tracker.get_issue(children["client"]).labels
    assert "approved" not in case.tracker.get_issue(children["api"]).labels
    assert case.tracker.link_calls == []
    assert case.tracker.sub_issue_reads == [parent]
    assert len(case.tracker.issues) == 3
    assert {case.tracker.parent_of(n) for n in children.values()} == {parent}
    assert {issue.number for issue in case.tracker.sub_issues(parent)} == set(children.values())
    assert case.tracker.parent_of(parent) is None


def test_order_republish_unchanged_plan_does_not_readd_links_or_dependencies(republish_case, capsys):
    case = republish_case
    code, message = publish(case, capsys)
    assert code == 0, message
    parent, children = published_issues(case)
    assert case.tracker.link_calls == [(parent, n) for n in children.values()]
    assert case.tracker.dependency_calls == [("add", children["client"], children["api"])]

    case.tracker.link_calls.clear()
    case.tracker.sub_issue_reads.clear()
    case.tracker.dependency_calls.clear()
    code, message = publish(case, capsys)
    assert code == 0, message
    assert case.tracker.link_calls == []
    assert case.tracker.sub_issue_reads == [parent]
    assert case.tracker.dependency_calls == []
    assert [dep.number for dep in case.tracker.blocked_by(children["client"])] == [children["api"]]
    assert len(case.tracker.issues) == 3


def test_order_republish_child_with_different_parent_still_fails(republish_case, capsys):
    case = republish_case
    code, message = publish(case, capsys)
    assert code == 0, message
    parent, children = published_issues(case)
    other_parent = case.tracker.create_issue("Another order", "Unrelated order.", [])
    child = children["api"]
    case.tracker.set_parent(child, other_parent)
    case.tracker.link_calls.clear()

    code, message = publish(case, capsys)
    assert code != 0
    assert LINK_ERROR in message
    assert case.tracker.link_calls == [(parent, child)]
    assert case.tracker.parent_of(child) == other_parent
    assert [issue.number for issue in case.tracker.sub_issues(other_parent)] == [child]
    assert "approved" not in case.tracker.get_issue(child).labels
