"""Edits of an issue published with behaviour statements: lock-only and carried-lock rules (#2558)."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import git_command
from foragent import executors, order_files, order_prepare_locks as locks
from foragent.executors import ExecResult
from helpers import lane_tests as _cases

lane_case, offline_publication = _cases.lane_case, _cases.offline_publication

NUMBER = 7
LOCKED = "tests/acceptance/test_notice_count.py"
LOCKED_CHECK = f"{sys.executable} {LOCKED}"
OLD = "Sending a notice reports one delivery."
CHANGED = "Sending a notice prints exactly one JSON object with one delivery."
GOAL = "A sent notice reports what it delivered."
SCOPE = "Delivery providers."


def _unit(body):
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


LOCKED_TEST = _unit('            self.assertEqual(json.loads(result.stdout)["deliveries"], 1)')
CORRECTED = _unit('            self.assertEqual(result.stdout.splitlines(), [json.dumps({"deliveries": 1})])')
REVISED = _unit('            self.assertEqual(result.stdout.splitlines(), [json.dumps({"deliveries": 1})])\n'
                '            self.assertEqual(result.stderr, "")')
PUBLISHED = {"goal": GOAL, "statements": [OLD], "out_of_scope": SCOPE, "files": ["notice.py"],
             "done_when": [LOCKED_CHECK], "checklist": ["A person reads the notice."],
             "acceptance_files": {LOCKED: LOCKED_TEST}}


def _body(**changes):
    return order_files.issue_body({**PUBLISHED, **changes}, "owner/repo", "main", "tests/acceptance/")


def _edit(**changes):
    edit = {"number": NUMBER, "goal": GOAL, "out_of_scope": SCOPE, "statements": [OLD],
            "files": ["notice.py"], "done_when": [LOCKED_CHECK], "checklist": ["A person reads the notice."],
            "acceptance_files": {LOCKED: CORRECTED}}
    edit.update(changes)
    return {name: value for name, value in edit.items() if value is not None}


# The exemption predicate ---------------------------------------------------------------------


def test_an_exact_restatement_that_replaces_an_existing_lock_is_lock_only():
    assert locks.facts(_edit(), _body(), "tests/acceptance/").get("lock_only") is True


@pytest.mark.parametrize("change", [
    {"checklist": ["A person reads the notice.", "A person checks the receipt."]},
    {"checklist": None},
    {"files": ["notice.py", "receipt.py"]},
    {"exclusive": ["notice.py"]},
    {"blocked_by": []},
    {"references": []},
    {"decisions": ["D1"]},
    {"done_when_removals": []},
    {"goal": "A sent notice reports its deliveries and receipts."},
    {"out_of_scope": "Nothing."},
    {"statements": [OLD, "Another statement."]},
    {"done_when": [LOCKED_CHECK, "true"]},
    {"acceptance_files": {LOCKED: CORRECTED, "tests/acceptance/test_new.py": "x = 1\n"}},
], ids=lambda change: next(iter(change)))
def test_any_other_change_beside_the_lock_is_not_lock_only(change):
    assert not locks.facts(_edit(**change), _body(), "tests/acceptance/").get("lock_only")


def test_a_published_body_with_references_is_not_restated_by_an_edit():
    body = _body().replace("\n## Done when", "\n## References\n- docs/x.md\n\n## Done when")
    assert not locks.facts(_edit(), body, "tests/acceptance/").get("lock_only")


def test_old_statements_count_as_covered_only_while_the_existing_lock_runs_unchanged():
    added = _edit(statements=[OLD, CHANGED], acceptance_files=None)
    assert locks.facts(added, _body(), "tests/acceptance/").get("covered") == [OLD]
    # A corrected lock no longer proves what the old one did; nor does a Done when without it.
    carried = _edit(statements=[OLD, CHANGED])
    assert "covered" not in locks.facts(carried, _body(), "tests/acceptance/")
    dropped = _edit(statements=[OLD, CHANGED], acceptance_files=None, done_when=["true"])
    assert "covered" not in locks.facts(dropped, _body(), "tests/acceptance/")


# Through the real commands -------------------------------------------------------------------


@pytest.fixture
def world(lane_case, git_history, monkeypatch):  # noqa: F811
    case = lane_case
    history = git_history
    history.update({LOCKED: LOCKED_TEST}, message="locked tests of #7")
    git_command(case.root, "fetch", "origin", "main")
    git_command(case.root, "restore", "--source=origin/main", "--staged", "--worktree", "--", ".")
    git_command(case.root, "update-ref", "refs/heads/main", history.tip)
    case.body = order_files.issue_body(PUBLISHED, "owner/repo", "main", case.config.acceptance_dir)
    case.tracker.add_issue(NUMBER, "Notice count", body=case.body, labels=["lane:api", "size:s"])
    case.calls, case.verdicts, case.writes = [], [], []

    def run(name, **kw):
        role = "writer" if name == "codex" else "reviewer"
        tree = Path(kw["worktree"])
        call = {"role": role, "seen": (tree / LOCKED).read_text() if (tree / LOCKED).is_file() else None}
        case.calls.append(call)
        if role == "writer":
            content = case.writes.pop(0) if case.writes else None
            if content is not None:
                (tree / LOCKED).write_text(content)
            message = {"acceptance_files": [LOCKED], "done_when": [LOCKED_CHECK], "questions": [], "notes": []}
        else:
            verdict = case.verdicts.pop(0) if case.verdicts else "pass"
            checked = subprocess.run(LOCKED_CHECK, shell=True, cwd=tree, capture_output=True, text=True)
            message = {"verdict": verdict, "reason": f"{verdict}: (d) the lock misses stderr.",
                       "questions": [], "notes": [],
                       "coverage": [{"statement": CHANGED, "tests": [LOCKED], "break_it": []}],
                       "red": [{"command": LOCKED_CHECK, "collected": 1, "failed": 1,
                                "exit_code": checked.returncode, "output": checked.stdout + checked.stderr or "x",
                                "failure_kind": "assertion"}]}
        log = Path(kw["log_path"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(message))
        return ExecResult(0, False, json.dumps(message), log, session_id=f"{role}-{len(case.calls)}",
                          error_kind="normal")

    for name, cls in (("codex", executors.CodexExecutor), ("claude", executors.ClaudeExecutor)):
        monkeypatch.setattr(cls, "run", lambda self, _name=name, **kw: run(_name, **kw))
    return case


def _save(case, edit):
    case.plan = {**case.plan, "issues": [], "edits": [edit]}
    case.save()


def _origin(case, path):
    git_command(case.root, "fetch", "origin", "main")
    shown = subprocess.run(["git", "show", f"origin/main:{path}"], cwd=case.root, capture_output=True, text=True)
    return shown.stdout if shown.returncode == 0 else None


def test_a_lock_fix_that_also_adds_a_checklist_line_is_not_published_without_review(world, capsys):
    case = world
    _save(case, _edit(checklist=["A person reads the notice.", "A person checks the receipt."]))
    code, out, err = _cases.publish(case, capsys)
    assert code != 0, out + err
    assert case.calls == []
    assert _origin(case, LOCKED) == LOCKED_TEST
    assert case.tracker.issues[NUMBER].body == case.body


def test_a_retry_after_a_hold_keeps_the_writers_revision_of_the_carried_lock(world, capsys):
    case = world
    _save(case, _edit(statements=[CHANGED]))
    case.writes = [REVISED]          # the first writer revises the carried lock; the second keeps it
    case.verdicts = ["hold", "pass"]
    for _ in range(2):  # the hold ends the first preparation; the next one retries the writer
        code, out, err = _cases.invoke(case, capsys, "prepare", str(case.path), "--json")
        assert code == 0, out + err
    row = json.loads(out)["issues"][0]
    assert row["status"] == "reviewed", row
    roles = [call["role"] for call in case.calls]
    assert roles == ["writer", "reviewer", "writer", "reviewer"], roles
    assert case.calls[0]["seen"] == CORRECTED, "The first writer starts from the plan's correction"
    assert case.calls[2]["seen"] == REVISED, "The retry starts from the writer's saved revision"
    assert case.calls[3]["seen"] == REVISED
    assert json.loads(case.path.read_text())["edits"][0]["acceptance_files"] == {LOCKED: REVISED}
    assert _origin(case, LOCKED) == LOCKED_TEST


def test_a_new_correction_in_the_plan_replaces_the_saved_revision(world, capsys):
    """The plan carrying other bytes than the held attempt was given is a new input, not a stale one."""
    case = world
    _save(case, _edit(statements=[CHANGED]))
    case.writes = [REVISED]
    case.verdicts = ["hold"]
    code, out, err = _cases.invoke(case, capsys, "prepare", str(case.path), "--json")
    assert code == 0, out + err
    newer = REVISED.replace('result.stderr, ""', 'result.returncode, 0')
    plan = json.loads(case.path.read_text())
    plan["edits"][0]["acceptance_files"] = {LOCKED: newer}
    case.path.write_text(json.dumps(plan))
    case.plan = plan
    case.verdicts = ["pass"]
    code, out, err = _cases.invoke(case, capsys, "prepare", str(case.path), "--json")
    assert code == 0, out + err
    assert case.calls[2]["role"] == "writer" and case.calls[2]["seen"] == newer
