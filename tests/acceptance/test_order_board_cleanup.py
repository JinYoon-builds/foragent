"""Acceptance for board cleanup through order publish. Locked by Foragent; do not edit.

Public contract: a plan may carry ``closes``, a list of existing issues the person agreed
to close, one item each. ``{"number": N, "commit": SHA}`` closes N as resolved by that
commit; ``{"number": N, "into": M}`` folds duplicate N into existing issue M. Closes are
written only with approval (``--approve``, or the item's number in ``--approve-numbers``).
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


RESOLVED, DUPLICATE, DECLINED = 12, 13, 14
FIX = "4f1c2a9e8b7d6c5e4f3a2b1c0d9e8f7a6b5c4d3e"


@pytest.fixture
def cleanup_case(publishing, monkeypatch):
    root, config, tracker, git, events = publishing
    config = replace(config, language="en", executor="codex", skill_libraries=[])
    monkeypatch.chdir(root)
    calls = record_gh(monkeypatch)

    def no_network(*args, **kwargs):
        pytest.fail("Board cleanup must use the offline tracker")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    tracker.add_issue(RESOLVED, "Login fails on empty password", "Crash on empty password.")
    tracker.add_issue(DUPLICATE, "Empty password crashes login", "Same crash, reported again.")
    # The person declined the proposed rewrite of this one, so the plan leaves it out.
    tracker.add_issue(DECLINED, "Dark mode", "Add dark mode.")
    value = {
        "id": "2026-11-04-board-cleanup", "title": "Clean up the existing board",
        "summary": "Close what history already resolved and fold a duplicate.",
        "spec_markdown": "# Board cleanup\n",
        "issues": [],
        "closes": [{"number": RESOLVED, "commit": FIX},
                   {"number": DUPLICATE, "into": RESOLVED}],
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


def existing(case):
    numbers = (RESOLVED, DUPLICATE, DECLINED)
    return ({n: case.tracker.get_issue(n) for n in numbers},
            {n: case.tracker.list_comments(n) for n in numbers})


def mentions(number, message):
    return re.search(rf"(?<!\d){number}(?!\d)", message)


def test_dry_run_previews_each_close_and_writes_nothing(cleanup_case, capsys):
    """Reject a silent preview, a preview without the evidence, and a dry run that closes."""
    case = cleanup_case
    before = deepcopy(case.tracker.issues)
    code, message = publish(case, capsys, "--approve", "--dry-run")
    assert code == 0, message
    assert mentions(RESOLVED, message) and mentions(DUPLICATE, message), message
    assert FIX[:7] in message, f"The preview must show the fixing commit: {message}"
    assert "close" in message.casefold(), message
    assert case.tracker.issues == before
    assert all(case.tracker.list_comments(n) == [] for n in before)
    assert case.events == [], "Dry run must not publish Git files or create issues"


def test_approved_cleanup_closes_exactly_the_agreed_items_once(cleanup_case, capsys):
    """Reject a skipped close, a close without evidence, a fold that does not name its target,
    touching a declined item, and a rerun that closes or comments again."""
    case = cleanup_case
    code, message = publish(case, capsys, "--approve")
    assert code == 0, message
    issues, comments = existing(case)
    assert {n for n, issue in issues.items() if issue.state == "closed"} == {RESOLVED, DUPLICATE}
    assert issues[RESOLVED].state_reason == "completed", "History resolved it: close as completed"
    assert len(comments[RESOLVED]) == 1 and FIX[:7] in comments[RESOLVED][0].body, (
        f"#{RESOLVED} needs one closing comment carrying the fixing commit: {comments[RESOLVED]}"
    )
    assert len(comments[DUPLICATE]) == 1, f"#{DUPLICATE} needs one closing comment"
    assert re.search(rf"(?:#|/issues/){RESOLVED}\b", comments[DUPLICATE][0].body), (
        f"The comment on duplicate #{DUPLICATE} must link to #{RESOLVED}"
    )
    assert issues[DECLINED].state == "open" and comments[DECLINED] == []
    assert issues[DECLINED].body == "Add dark mode."

    code, message = publish(case, capsys, "--approve")
    assert code == 0, f"An already-applied cleanup is a successful no-op: {message}"
    assert existing(case) == (issues, comments), "Publishing the same plan again must write nothing more"


@pytest.mark.parametrize("approval", [[], ["--approve-numbers", str(RESOLVED)]],
                         ids=["no-approval", "one-item-approved"])
def test_each_close_waits_for_its_own_approval(cleanup_case, capsys, approval):
    """Reject treating a draft publication, or another item's approval, as consent to close."""
    case = cleanup_case
    code, message = publish(case, capsys, *approval)
    assert code == 0, message
    issues, comments = existing(case)
    approved = {RESOLVED} if approval else set()
    assert {n for n, issue in issues.items() if issue.state == "closed"} == approved
    for number in (DUPLICATE, DECLINED):
        assert comments[number] == [], f"Unapproved #{number} must get no GitHub write"


def test_running_target_refuses_the_whole_cleanup(cleanup_case, capsys):
    """Reject closing work that is running, and closing the other items before refusing."""
    case = cleanup_case
    case.tracker.add_labels(DUPLICATE, ["running"])
    code, message = publish(case, capsys, "--approve")
    assert code != 0, f"Publish must refuse to close running #{DUPLICATE}"
    assert mentions(DUPLICATE, message) and "running" in message.casefold(), message
    issues, comments = existing(case)
    assert all(issue.state == "open" for issue in issues.values())
    assert all(found == [] for found in comments.values())

