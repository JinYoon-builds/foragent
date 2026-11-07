"""Acceptance tests for prepare-counts-existing-locks.

Issue #7 is already published on the statement route: its body has a Behaviour section and its
Done when runs a locked acceptance file that is on main. An order edits it in one of three ways:

1. it adds one statement beside the old one, which the existing locked file covers;
2. it changes nothing but a corrected version of the existing locked file (lock-only fix);
3. it changes the statement and carries a corrected version of the existing locked file.

Everything runs through `fa order prepare` and `fa order publish` on the offline toy project of
`lane_tests_case` (real local Git with a bare origin, a fake tracker). The writer and reviewer
are scripted executors; the reviewer really runs each known command in its scratch tree, so
its exit codes agree with the ones prepare measures. No network, signal or real agent is used.
"""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import git_command
from foragent.executors import ExecResult
from foragent import executors, order_files
from lane_tests_case import (CANDIDATE, CHECK, STATEMENT, TEST_PATH, invoke, lane_case,  # noqa: F401
                             offline_publication, publish)

NUMBER = 7
LOCKED = "tests/acceptance/test_notice_count.py"
LOCKED_CHECK = f"{sys.executable} {LOCKED}"
OLD_STATEMENT = "Sending a notice reports one delivery."
CHANGED_STATEMENT = "Sending a notice prints exactly one JSON object with one delivery."
GOAL = "A sent notice reports what it delivered."
SCOPE = "Delivery providers."


def _unit(body: str) -> str:
    return f'''import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class Count(unittest.TestCase):
    def test_count(self):
        source = Path(__file__).resolve().parents[2] / "notice.py"
        with tempfile.TemporaryDirectory() as output:
            result = subprocess.run([sys.executable, str(source), "send"], cwd=output,
                                    capture_output=True, text=True, check=True)
{body}


if __name__ == "__main__":
    unittest.main()
'''


# The locked test on main (red on main: the issue is not implemented yet) and its correction.
LOCKED_TEST = _unit('            self.assertEqual(json.loads(result.stdout)["deliveries"], 1)')
CORRECTED = _unit('            self.assertEqual(result.stdout.splitlines(), [json.dumps({"deliveries": 1})])')
KNOWN_COMMANDS = (LOCKED_CHECK, CHECK)


def _published(case):
    """Put the locked file on main and publish issue #7 with it, as the statement route did."""
    root = case.root
    history = case.history
    history.update({LOCKED: LOCKED_TEST}, message="locked tests of #7")
    git_command(root, "fetch", "origin", "main")
    git_command(root, "restore", "--source=origin/main", "--staged", "--worktree", "--", ".")
    git_command(root, "update-ref", "refs/heads/main", history.tip)
    body = order_files.issue_body(
        {"goal": GOAL, "statements": [OLD_STATEMENT], "out_of_scope": SCOPE, "files": ["notice.py"],
         "done_when": [LOCKED_CHECK], "acceptance_files": {LOCKED: LOCKED_TEST}},
        "owner/repo", "main", case.config.acceptance_dir)
    case.tracker.add_issue(NUMBER, "Notice count", body=body, labels=["lane:api", "size:s"])
    return body


def _script(case, monkeypatch):
    """Scripted writer and reviewer whose behaviour each test sets on the case."""
    case.writer_files = {TEST_PATH: CANDIDATE}
    case.writer_reply_files = [TEST_PATH]
    case.writer_done_when = [CHECK]
    case.coverage = [{"statement": STATEMENT, "tests": [TEST_PATH],
                      "break_it": ["wrong value", "skipped effect", "doubled effect"]}]

    def run(name, **kw):
        role = "writer" if name == "codex" else "reviewer"
        worktree = Path(kw["worktree"])
        call = {"role": role, "executor": name}
        case.calls.append(call)
        if role == "writer":
            for relative, content in case.writer_files.items():
                path = worktree / relative
                # Leave a file alone when it already has the wanted bytes (a kept candidate).
                if not path.is_file() or path.read_text() != content:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
            message = {"acceptance_files": list(case.writer_reply_files),
                       "done_when": list(case.writer_done_when), "questions": [], "notes": []}
        else:
            call["seen"] = {relative: (worktree / relative).read_text()
                            for relative in (LOCKED, TEST_PATH) if (worktree / relative).is_file()}
            red = []
            for command in KNOWN_COMMANDS:
                checked = subprocess.run(command, shell=True, cwd=worktree, capture_output=True, text=True)
                red.append({"command": command, "collected": 1, "failed": 1, "exit_code": checked.returncode,
                            "output": (checked.stdout + checked.stderr) or "no output",
                            "failure_kind": "assertion"})
            message = {"verdict": "pass", "reason": "The statements are observable; break-it checked.",
                       "questions": [], "notes": [], "coverage": case.coverage, "red": red}
        log = Path(kw["log_path"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(message))
        return ExecResult(0, False, json.dumps(message), log, session_id=f"{role}-{len(case.calls)}",
                          error_kind="normal")

    for name, cls in (("codex", executors.CodexExecutor), ("claude", executors.ClaudeExecutor)):
        monkeypatch.setattr(cls, "run", lambda self, _name=name, **kw: run(_name, **kw))


@pytest.fixture
def world(lane_case, git_history, monkeypatch):  # noqa: F811
    case = lane_case
    case.history = git_history
    case.body = _published(case)
    _script(case, monkeypatch)
    return case


def _edit(case, **fields):
    edit = {"number": NUMBER, "goal": GOAL, "out_of_scope": SCOPE, "statements": [OLD_STATEMENT],
            "files": ["notice.py"], "done_when": [LOCKED_CHECK]}
    edit.update(fields)
    case.plan = {**case.plan, "issues": [], "edits": [edit]}
    case.save()
    return edit


def _prepare(case, capsys):
    code, out, err = invoke(case, capsys, "prepare", str(case.path), "--json")
    assert code == 0, out + err
    rows = json.loads(out)["issues"]
    assert [row["key"] for row in rows] == [f"#{NUMBER}"], rows
    return rows[0]


def _plan_edit(case):
    edits = json.loads(case.path.read_text())["edits"]
    assert len(edits) == 1
    return edits[0]


def _roles(case):
    return [call["role"] for call in case.calls]


def _origin_main_file(case, path):
    git_command(case.root, "fetch", "origin", "main")
    result = subprocess.run(["git", "show", f"origin/main:{path}"], cwd=case.root,
                            capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


# Statement 1 -------------------------------------------------------------------------------


def test_an_added_statement_is_reviewed_with_one_new_file_beside_the_existing_lock(world, capsys):
    case = world
    _edit(case, statements=[OLD_STATEMENT, STATEMENT])
    row = _prepare(case, capsys)
    assert row["status"] == "reviewed", row
    assert _roles(case) == ["writer", "reviewer"], "One writer and one reviewer, each once"
    files = _plan_edit(case)["acceptance_files"]
    assert files.get(TEST_PATH) == CANDIDATE, files
    assert set(files) <= {TEST_PATH, LOCKED}, files
    assert files.get(LOCKED, LOCKED_TEST) == LOCKED_TEST, "The existing lock is not rewritten"
    assert _origin_main_file(case, LOCKED) == LOCKED_TEST
    assert case.tracker.issues[NUMBER].body == case.body, "Preparation publishes nothing"


def test_the_new_statement_itself_still_needs_coverage_by_the_new_file(world, capsys):
    """Control: the existing lock does not cover a statement the published issue never had."""
    case = world
    _edit(case, statements=[OLD_STATEMENT, STATEMENT])
    case.coverage = [{"statement": OLD_STATEMENT, "tests": [LOCKED], "break_it": []}]
    row = _prepare(case, capsys)
    assert row["status"] != "reviewed", row
    assert "acceptance_files" not in _plan_edit(case) or not _plan_edit(case)["acceptance_files"]


def test_a_changed_statement_is_not_covered_by_the_existing_lock(world, capsys):
    """Control: only statements unchanged from the published issue count as covered."""
    case = world
    _edit(case, statements=[CHANGED_STATEMENT, STATEMENT])
    row = _prepare(case, capsys)
    assert row["status"] != "reviewed", row


# Statement 2 -------------------------------------------------------------------------------


def test_a_lock_only_edit_publishes_without_test_preparation(world, capsys):
    case = world
    _edit(case, acceptance_files={LOCKED: CORRECTED})
    code, out, err = publish(case, capsys)
    assert code == 0, out + err
    assert case.calls == [], "No writer or reviewer runs for a lock-only fix"
    assert _origin_main_file(case, LOCKED) == CORRECTED
    issue = case.tracker.issues[NUMBER]
    assert "## Behaviour\n- " + OLD_STATEMENT in issue.body, issue.body
    assert LOCKED_CHECK in issue.body, issue.body


def test_a_lock_fix_with_a_changed_goal_still_needs_prepared_tests(world, capsys):
    """Control: only an edit that changes nothing but existing locked files skips preparation."""
    case = world
    _edit(case, goal="A sent notice reports its deliveries and receipts.", acceptance_files={LOCKED: CORRECTED})
    code, out, err = publish(case, capsys)
    assert code != 0, out + err
    assert _origin_main_file(case, LOCKED) == LOCKED_TEST
    assert case.tracker.issues[NUMBER].body == case.body


def test_a_new_acceptance_file_is_not_a_lock_only_fix(world, capsys):
    """Control: a file that is not one of the issue's existing locks still needs preparation."""
    case = world
    _edit(case, acceptance_files={TEST_PATH: CANDIDATE})
    code, out, err = publish(case, capsys)
    assert code != 0, out + err
    assert _origin_main_file(case, TEST_PATH) is None
    assert case.tracker.issues[NUMBER].body == case.body


# Statement 3 -------------------------------------------------------------------------------


def test_a_changed_statement_with_a_corrected_lock_is_reviewed_with_that_file(world, capsys):
    case = world
    _edit(case, statements=[CHANGED_STATEMENT], acceptance_files={LOCKED: CORRECTED})
    case.writer_files = {LOCKED: CORRECTED}
    case.writer_reply_files = [LOCKED]
    case.writer_done_when = [LOCKED_CHECK]
    case.coverage = [{"statement": CHANGED_STATEMENT, "tests": [LOCKED],
                      "break_it": ["wrong value", "skipped effect", "doubled effect"]}]
    row = _prepare(case, capsys)
    assert row["status"] == "reviewed", row
    assert row.get("written") == [LOCKED], row
    assert _roles(case) == ["writer", "reviewer"], "One writer and one reviewer, each once"
    reviewer = case.calls[-1]
    assert reviewer["seen"].get(LOCKED) == CORRECTED, "The reviewer ran the corrected lock, not main's"
    edit = _plan_edit(case)
    assert edit["acceptance_files"] == {LOCKED: CORRECTED}, edit
    assert LOCKED_CHECK in edit["done_when"], edit
    assert _origin_main_file(case, LOCKED) == LOCKED_TEST, "Preparation publishes nothing"
