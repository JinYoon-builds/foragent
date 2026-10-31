"""Acceptance for order-replaces. Locked by Foragent; do not edit.

Public contract: new plan issues may list existing issue numbers in ``replaces``.
Each test's docstring names the wrong implementation it must reject.
Only Git, tracker and executor service boundaries use existing offline fixtures.
"""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import re
import socket
from types import SimpleNamespace

import pytest

from test_local_truth import record_gh
from test_order import publishing  # noqa: F401
from test_publish_out_of_scope import publish_cli
from test_publish_own_paths import with_clean_preread
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


REPLACED = (41, 42)
UNRELATED = 43


@pytest.fixture
def replacement_case(publishing, monkeypatch):
    root, config, tracker, git, events = publishing
    config = replace(config, language="en", executor="codex", skill_libraries=[])
    monkeypatch.chdir(root)
    calls = record_gh(monkeypatch)

    def no_network(*args, **kwargs):
        pytest.fail("Replacement publication must use the offline tracker")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    for number in (*REPLACED, UNRELATED):
        tracker.add_issue(number, f"Existing work {number}", "Keep the original discussion.")
    value = {
        "id": "2026-10-31-replacement-check", "title": "Replace old work",
        "summary": "The person chose new work instead of these existing issues.",
        "spec_markdown": "# Replacement work\n",
        "issues": [{
            "key": "replacement", "title": "Deliver the replacement",
            "goal": "Deliver the newly chosen work.", "out_of_scope": "Other work.",
            "done_when": ["true"], "lane": "api", "size": "s",
            "acceptance_files": {}, "replaces": list(REPLACED),
        }],
    }
    main = with_clean_preread(
        publish_cli((root, config, tracker, git, events), monkeypatch), monkeypatch,
    )
    return SimpleNamespace(root=root, tracker=tracker, events=events, plan=value,
                           path=root / "PLAN.json", main=main, gh=calls)


def publish(case, capsys, *options):
    case.path.write_text(json.dumps(case.plan), encoding="utf-8")
    capsys.readouterr()
    code = cli_exit(case.main, ["order", "publish", str(case.path),
                                "--repo-root", str(case.root), *options])
    captured = capsys.readouterr()
    assert case.gh == [], "No GitHub process may run in these acceptance tests"
    return code, captured.out + captured.err


def successors(case):
    return [next(issue for issue in case.tracker.list_issues()
                 if issue.title == item["title"]) for item in case.plan["issues"]]


def assert_replaced(case):
    children = successors(case)
    assert all(issue.state == "open" and "approved" in issue.labels for issue in children)
    for number in REPLACED:
        old = case.tracker.get_issue(number)
        assert old.state == "closed", f"Approved replacement must close #{number}"
        assert old.state_reason == "not_planned", f"#{number} must close as not planned"
        comments = case.tracker.list_comments(number)
        assert len(comments) == 1, f"#{number} must get one replacement comment"
        for child in children:
            assert re.search(rf"(?:#|/issues/){child.number}\b", comments[0].body), (
                f"The comment on #{number} must link to replacement #{child.number}"
            )
    assert case.tracker.get_issue(UNRELATED).state == "open"
    assert case.tracker.list_comments(UNRELATED) == []


def test_dry_run_lists_closures_without_closing_anything(replacement_case, capsys):
    """Reject a silent preview and a dry run that performs real closures."""
    case = replacement_case
    before = deepcopy(case.tracker.issues)
    code, message = publish(case, capsys, "--approve", "--dry-run")
    assert code == 0, message
    for number in REPLACED:
        assert re.search(rf"(?<!\d){number}(?!\d)", message), message
    preview = message.casefold().replace("_", " ").replace("-", " ")
    assert "close" in preview and "not planned" in preview, message
    assert case.tracker.issues == before
    assert all(case.tracker.list_comments(number) == [] for number in before)
    assert case.events == [], "Dry run must not publish Git files or create issues"


def test_publish_without_approval_leaves_replaced_issues_open(replacement_case, capsys):
    """Reject treating draft publication as the person's consent to close work."""
    case = replacement_case
    code, message = publish(case, capsys)
    assert code == 0, message
    assert all("approved" not in issue.labels for issue in successors(case))
    for number in (*REPLACED, UNRELATED):
        assert case.tracker.get_issue(number).state == "open"
        assert case.tracker.list_comments(number) == []


def test_approved_republish_closes_once_and_links_all_successors(replacement_case, capsys):
    """Reject ignored replacements, completed closures, missing links and duplicate comments."""
    case = replacement_case
    second = deepcopy(case.plan["issues"][0])
    second.update(key="other-replacement", title="Deliver the other replacement")
    case.plan["issues"].append(second)
    code, message = publish(case, capsys, "--approve")
    assert code == 0, message
    assert_replaced(case)
    comments = {number: case.tracker.list_comments(number) for number in REPLACED}
    children = [issue.number for issue in successors(case)]
    count = len(case.tracker.issues)

    code, message = publish(case, capsys, "--approve")
    assert code == 0, f"An already-closed replacement is a successful no-op: {message}"
    assert_replaced(case)
    assert {number: case.tracker.list_comments(number) for number in REPLACED} == comments
    assert [issue.number for issue in successors(case)] == children
    assert len(case.tracker.issues) == count


@pytest.mark.parametrize("conflict", ["running", "edited", "approved"])
def test_conflicting_replacement_is_named_and_refused(replacement_case, capsys, conflict):
    """Reject closing running work, an edited issue or a sibling approved by this order."""
    case = replacement_case
    target = REPLACED[-1]
    if conflict == "running":
        case.tracker.add_labels(target, ["running"])
        reason = "running"
    elif conflict == "edited":
        case.plan["edits"] = [{
            "number": target, "goal": "Revise this existing work.",
            "out_of_scope": "Other work.", "done_when": ["true"], "acceptance_files": {},
        }]
        reason = "edit"
    else:
        # Publish an ordinary draft first, so the sibling has a real issue number.
        for item in case.plan["issues"]:
            item.pop("replaces")
        code, message = publish(case, capsys)
        assert code == 0, message
        target = successors(case)[0].number
        other = deepcopy(case.plan["issues"][0])
        other.update(key="other-replacement", title="Supersede the sibling", replaces=[target])
        case.plan["issues"].append(other)
        reason = "approv"

    code, message = publish(case, capsys, "--approve")
    assert code != 0, f"Publish must refuse the {conflict} replacement #{target}"
    assert re.search(rf"(?<!\d){target}(?!\d)", message), message
    assert reason in message.casefold(), f"Explain the {conflict} conflict: {message}"
    for number in (*REPLACED, UNRELATED, target):
        assert case.tracker.get_issue(number).state == "open", (
            f"Refusing #{target} must leave #{number} open"
        )


def test_manual_reopen_survives_an_unrelated_order(replacement_case, capsys):
    """Reject remembering replacements as a permanent instruction to close reopened work."""
    case = replacement_case
    code, message = publish(case, capsys, "--approve")
    assert code == 0, message
    assert_replaced(case)
    number = REPLACED[0]
    comments = case.tracker.list_comments(number)
    # A human's tracker-side reopen, outside any Foragent command.
    case.tracker.issues[number] = replace(case.tracker.get_issue(number),
                                          state="open", state_reason=None)
    case.plan.update(id="2026-10-31-unrelated-check", title="Unrelated work",
                     summary="Deliver unrelated work.", spec_markdown="# Unrelated work\n")
    case.plan["issues"][0].update(key="unrelated", title="Deliver unrelated work",
                                 goal="Deliver unrelated work.")
    case.plan["issues"][0].pop("replaces")

    code, message = publish(case, capsys, "--approve")
    assert code == 0, message
    assert all("approved" in issue.labels for issue in successors(case))
    reopened = case.tracker.get_issue(number)
    assert reopened.state == "open" and reopened.state_reason is None
    assert case.tracker.list_comments(number) == comments
    assert case.tracker.get_issue(REPLACED[-1]).state == "closed"


def test_planning_skill_lists_replacements_instead_of_requesting_manual_closure():
    """Reject implementing the CLI while planning still delegates closures to the person."""
    root = next(parent for parent in Path(__file__).resolve().parents
                if (parent / "skills/fa-order/SKILL.md").is_file())
    lines = (root / "skills/fa-order/SKILL.md").read_text(encoding="utf-8").splitlines()
    assert any(all(term in line.casefold().replace("`", "") for term in (
        "replaced issues", "plan", "issues[].replaces", "instead of asking", "person", "close",
    )) for line in lines), (
        "fa-order needs one line telling planning to list replaced issues in the plan's "
        "issues[].replaces instead of asking the person to close them"
    )
