"""Acceptance test for test-harness-guard. Locked by Foragent; do not edit."""

import shlex

import pytest

from conftest import git_command
from foragent import runner
from foragent.issue import body_hash
from foragent.reasons import parse_reason
from test_base_move_reuse import MERGED, UNOWNED, publish_case  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


HOLD = "test-harness-changed"
SESSIONFINISH = '''import pytest
from pathlib import Path
import xml.etree.ElementTree as ET


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    report = getattr(session.config.option, "xmlpath", None)
    if report and Path(report).is_file():
        tree = ET.parse(report)
        for suite in tree.iter("testsuite"):
            suite.set("failures", "0")
            suite.set("errors", "0")
        for case in tree.iter("testcase"):
            for tag in ("failure", "error"):
                for failure in case.findall(tag):
                    case.remove(failure)
        tree.write(report, encoding="utf-8", xml_declaration=True)
    session.exitstatus = 0
'''


@pytest.fixture
def harness_case(publish_case, monkeypatch):
    """Keep real verification; supply GitHub's file list from the real PR diff."""
    case = publish_case
    create_pr = case.tracker.create_pr

    def create(**kwargs):
        number = create_pr(**kwargs)
        git = case.git.at(case.store.worktree_path(1))
        case.tracker.set_pr_files(number, git.changed_paths(case.base_sha))
        return number

    monkeypatch.setattr(case.tracker, "create_pr", create)

    # Observe successful head comparisons even if a later guard holds the PR.
    # Base capture uses a separate worktree and must remain permitted.
    case.head_comparisons = []
    check = runner._check

    def observe_check(command, worktree, *args, **kwargs):
        result = check(command, worktree, *args, **kwargs)
        if worktree == case.store.worktree_path(1) and any(
            word.startswith((f"{MERGED}::", f"{UNOWNED}::"))
            for word in shlex.split(command)
        ):
            case.head_comparisons.append(result)
        return result

    monkeypatch.setattr(runner, "_check", observe_check)
    return case


def implement_files(case, changes, *, listed=()):
    approval = case.store.load_approval(1)
    files = "## Files\n- feature.txt\n" + "".join(f"- {path}\n" for path in listed)
    body = approval.body.replace("## Done when\n", files + "\n## Done when\n")
    case.tracker.edit_body(1, body)
    case.store.save_approval(
        1, body=body, body_sha=body_hash(body), acceptance_blobs=approval.acceptance_blobs,
        approved_by="human", approved_at=case.clock[0].isoformat(),
    )

    def implement(kwargs):
        case.implement(kwargs)
        for name, content in changes.items():
            path = kwargs["worktree"] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return "Implemented feature and changed test configuration"

    case.implementer.steps = [implement]


def assert_held(case, paths):
    outcome = runner.run_issue(1, case.ctx)
    assert outcome == "held", (
        f"Unlisted test-harness changes {sorted(paths)} must be held before trusting "
        f"head acceptance evidence; got {outcome!r}"
    )
    assert case.store.get_run(1).outcome == "held"
    flag = case.store.load_flag(1)
    assert flag is not None and flag.reason == HOLD
    comments = [comment.body for comment in case.tracker.list_comments(1)
                if parse_reason(comment.body) == HOLD]
    assert any(all(path in comment for path in paths) for comment in comments), (
        f"The {HOLD} comment must name every changed harness path: {sorted(paths)}"
    )
    assert case.tracker.merged_prs == []
    pr = case.tracker.find_pr("foragent/issue-1")
    assert pr is not None, "Preserve an open PR for human review"
    assert case.tracker.get_issue(pr).state == "open"
    issue = case.tracker.get_issue(1)
    assert issue.state == "open" and "needs-human" in issue.labels
    assert not any(result.exit_code == 0 and not result.timed_out
                   for result in case.head_comparisons), (
        "A later publication hold cannot undo an already accepted head comparison"
    )


def assert_merged(case, changes):
    outcome = runner.run_issue(1, case.ctx)
    assert outcome == "merged", case.store.log_path(1).read_text()
    assert case.store.get_run(1).outcome == "merged"
    assert len(case.tracker.merged_prs) == 1
    assert case.tracker.get_issue(1).state == "closed"
    assert "needs-human" not in case.tracker.get_issue(1).labels
    assert case.store.load_flag(1) is None
    assert case.head_comparisons, "The control must exercise real head acceptance verification"
    assert all(result.exit_code == 0 and not result.timed_out for result in case.head_comparisons)
    head = case.tracker.pr_head_sha(case.tracker.merged_prs[0])
    for path, content in changes.items():
        assert git_command(case.history.origin, "show", f"{head}:{path}") == content.strip()


@pytest.mark.parametrize("changes", [
    pytest.param({"tests/sub/conftest.py": SESSIONFINISH}, id="nested-sessionfinish"),
    pytest.param({"tools/pytest.ini": "[pytest]\naddopts = --strict-markers\n"}, id="nested-pytest-ini"),
    pytest.param({"tools/tox.ini": "[pytest]\naddopts = --strict-markers\n"}, id="nested-tox-ini"),
    pytest.param({"setup.cfg": "[tool:pytest]\naddopts = --strict-markers\n"}, id="setup-cfg"),
    pytest.param({"tools/sitecustomize.py": "# Interpreter customization\n"}, id="nested-sitecustomize"),
    pytest.param({"tools/usercustomize.py": "# User customization\n"}, id="nested-usercustomize"),
    pytest.param({"tests/sub/conftest.py": SESSIONFINISH,
                  "setup.cfg": "[tool:pytest]\naddopts = --strict-markers\n"}, id="all-paths-in-comment"),
])
def test_unlisted_harness_paths_are_held(harness_case, changes):
    implement_files(harness_case, changes)
    assert_held(harness_case, changes)


@pytest.mark.parametrize("table", [
    pytest.param('[tool.pytest]\naddopts = ["--strict-markers"]\n', id="pytest"),
    pytest.param('[tool.pytest.ini_options]\naddopts = "--strict-markers"\n', id="pytest-ini-options"),
    pytest.param('[project.entry-points.pytest11]\nguard_probe = "guard_probe"\n', id="pytest11"),
])
def test_unlisted_pyproject_pytest_tables_are_held(harness_case, table):
    case = harness_case
    changes = {"pyproject.toml": (case.root / "pyproject.toml").read_text() + "\n" + table}
    implement_files(case, changes)
    assert_held(case, changes)


def test_pyproject_dependency_only_change_merges(harness_case):
    case = harness_case
    changes = {"pyproject.toml": (case.root / "pyproject.toml").read_text() + 'dependencies = ["pyyaml>=6"]\n'}
    implement_files(case, changes)
    assert_merged(case, changes)


def test_explicitly_listed_conftest_change_merges(harness_case):
    changes = {"tests/conftest.py": SESSIONFINISH}
    implement_files(harness_case, changes, listed=["tests/conftest.py"])
    assert_merged(harness_case, changes)
