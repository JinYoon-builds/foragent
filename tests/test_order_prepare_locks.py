"""Edits of an issue published with behaviour statements: lock-only and carried-lock rules (#2558)."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import executors, order, order_files, order_prepare_locks as locks, order_publications
from foragent.executors import ExecResult
from foragent.issue import MARKER
from foragent.order_references import repository_link
from helpers import lane_tests as _cases

lane_case, offline_publication = _cases.lane_case, _cases.offline_publication

NUMBER = 7
LOCKED = "tests/acceptance/test_notice_count.py"
HELPER = "tests/acceptance/notice_helper.py"
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


def _facts(item, body):
    return locks.facts(item, body, "tests/acceptance/", repo="owner/repo", base="main")


# The exemption predicate ---------------------------------------------------------------------


def test_an_exact_restatement_that_replaces_an_existing_lock_is_lock_only():
    assert _facts(_edit(), _body()).get("lock_only") is True


@pytest.mark.parametrize("marker", [False, True])
@pytest.mark.parametrize("link", [False, True])
def test_generated_scaffolding_is_optional_for_a_lock_only_correction(marker, link):
    body = _body()
    if not marker:
        body = body.removeprefix(MARKER + "\n")
    if not link:
        body = body.removesuffix(repository_link(LOCKED, "owner/repo", "main") + "\n")
    assert _facts(_edit(), body).get("lock_only") is True


@pytest.mark.parametrize("omitted", [[], [LOCKED], ["tests/acceptance/test_receipt.py"],
                                   [LOCKED, "tests/acceptance/test_receipt.py"]])
def test_each_existing_lock_link_is_optional_even_when_only_one_lock_is_corrected(omitted):
    other = "tests/acceptance/test_receipt.py"
    commands = [LOCKED_CHECK, f"{sys.executable} {other}"]
    body = _body(done_when=commands, acceptance_files={LOCKED: LOCKED_TEST, other: "assert False\n"})
    for name in omitted:
        body = body.replace(repository_link(name, "owner/repo", "main") + "\n", "")
    assert _facts(_edit(done_when=commands), body).get("lock_only") is True


@pytest.mark.parametrize("field", ["goal", "out_of_scope"])
@pytest.mark.parametrize("preserved", [False, True])
def test_a_locked_file_link_inside_authored_text_must_be_preserved(field, preserved):
    value = {"goal": GOAL, "out_of_scope": SCOPE}[field] + "\n" + repository_link(LOCKED, "owner/repo", "main")
    body = _body(**{field: value})
    item = _edit(**({field: value} if preserved else {}))
    assert bool(_facts(item, body).get("lock_only")) is preserved


@pytest.mark.parametrize("name", [LOCKED, "tests/acceptance/test_unlocked.py"])
def test_extra_or_unlocked_footer_links_are_not_lock_only(name):
    body = _body() + repository_link(name, "owner/repo", "main") + "\n"
    assert not _facts(_edit(), body).get("lock_only")


@pytest.mark.parametrize("change", [
    {"checklist": ["A person reads the notice.", "A person checks the receipt."]},
    {"checklist": None},
    {"files": ["notice.py", "receipt.py"]},
    {"exclusive": ["notice.py"]},
    {"blocked_by": []},
    {"references": []},
    {"decisions": ["D1"]},
    {"done_when_removals": []},
    {"title": "New notice title"},
    {"lane": "new-lane"},
    {"size": "l"},
    {"interactive": True},
    {"state": "open"},
    {"future_field": "New issue metadata"},
    {"goal": "A sent notice reports its deliveries and receipts."},
    {"out_of_scope": "Nothing."},
    {"statements": [OLD, "Another statement."]},
    {"done_when": [LOCKED_CHECK, "true"]},
    {"acceptance_files": {LOCKED: CORRECTED, "tests/acceptance/test_new.py": "x = 1\n"}},
], ids=lambda change: next(iter(change)))
def test_any_other_change_beside_the_lock_is_not_lock_only(change):
    assert not _facts(_edit(**change), _body()).get("lock_only")


def test_a_published_body_with_references_is_not_restated_by_an_edit():
    body = _body().replace("\n## Done when", "\n## References\n- docs/x.md\n\n## Done when")
    assert not _facts(_edit(), body).get("lock_only")


@pytest.mark.parametrize("extra", [
    "## Waiting\nReceipt design is not approved.",
    "## Design review\nReceipt design is not approved.",
    "## Goal\nReceipt design is not approved.",
    "Receipt design is not approved.",
    "<!-- Receipt design is not approved. -->",
    "## Preparation notes\nA person must approve the receipt design.",
], ids=["waiting", "unknown-section", "duplicate-section", "unsectioned", "comment", "preparation-notes"])
def test_removing_any_body_content_while_correcting_a_lock_needs_preparation(extra):
    from foragent.config import Config

    config = Config(repo="owner/repo")
    body = _body() + "\n" + extra + "\n"
    issue = SimpleNamespace(body=body, state="open", title="Notice", labels=["size:s"], number=NUMBER)
    item = order.edit_view(_edit(), issue, config)
    git = SimpleNamespace(head_sha=lambda: "main", file_at=lambda *args: LOCKED_TEST)
    assert not locks.lock_only(item)
    assert order_publications._lock_correction_files(item, issue, {LOCKED: CORRECTED}, git, config) is None


@pytest.mark.parametrize("change,state,exempt", [
    ({}, "open", True),
    ({"checklist": ["A person reads the notice.", "A person checks the receipt."]}, "open", False),
    *[({field: value}, "open", False)
      for field, original in (("goal", GOAL), ("out_of_scope", SCOPE))
      for value in (original + "\n", "\n" + original, original + " ")],
    ({}, "closed", False),
])
def test_preparation_and_branch_proof_share_the_exact_exemption(change, state, exempt):
    from foragent.config import Config

    config = Config(repo="owner/repo")
    issue = SimpleNamespace(body=_body(), state=state, title="Notice", labels=["size:s"], number=NUMBER)
    item = order.edit_view(_edit(**change), issue, config)
    git = SimpleNamespace(head_sha=lambda: "main", file_at=lambda *args: LOCKED_TEST)
    assert locks.lock_only(item) is exempt
    previous = order_publications._lock_correction_files(item, issue, {LOCKED: CORRECTED}, git, config)
    assert (previous is not None) is exempt


@pytest.mark.parametrize("field", ["goal", "out_of_scope"])
def test_unchanged_authored_whitespace_is_restatement(field):
    value = {"goal": GOAL, "out_of_scope": SCOPE}[field] + "\n "
    assert _facts(_edit(**{field: value}), _body(**{field: value}))["lock_only"]


@pytest.mark.parametrize("heading", ["Waiting", "Design review"])
def test_preserved_extra_sections_are_still_lock_only(heading):
    goal = GOAL + f"\n\n## {heading}\nReceipt design is not approved."
    assert _facts(_edit(goal=goal), _body(goal=goal))["lock_only"]


def test_changing_a_checklist_mark_while_correcting_a_lock_needs_preparation():
    body = _body().replace("- [ ] A person reads", "- [x] A person reads")
    assert not _facts(_edit(), body).get("lock_only")


@pytest.mark.parametrize("repo,base,exempt", [
    ("team/notice", "release/v2", True),
    ("other/notice", "release/v2", False),
    ("team/notice", "main", False),
    (None, "release/v2", False),
])
def test_restatement_compares_repository_links_with_the_publication_context(repo, base, exempt):
    from foragent.config import Config

    config = Config(repo=repo, base_branch=base)
    body = order_files.issue_body(PUBLISHED, "team/notice", "release/v2", config.acceptance_dir)
    issue = SimpleNamespace(body=body, state="open", title="Notice", labels=["size:s"], number=NUMBER)
    assert locks.lock_only(order.edit_view(_edit(), issue, config)) is exempt


def test_old_statements_count_as_covered_only_while_the_existing_lock_runs_unchanged():
    added = _edit(statements=[OLD, CHANGED], acceptance_files=None)
    assert _facts(added, _body()).get("covered") == [OLD]
    # A corrected lock no longer proves what the old one did; nor does a Done when without it.
    carried = _edit(statements=[OLD, CHANGED])
    assert "covered" not in _facts(carried, _body())
    dropped = _edit(statements=[OLD, CHANGED], acceptance_files=None, done_when=["true"])
    assert "covered" not in _facts(dropped, _body())


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
    case.reply_files = [LOCKED]

    def run(name, **kw):
        role = "writer" if name == "codex" else "reviewer"
        tree = Path(kw["worktree"])
        call = {"role": role, "seen": (tree / LOCKED).read_text() if (tree / LOCKED).is_file() else None}
        call["helper"] = (tree / HELPER).read_text() if (tree / HELPER).is_file() else None
        case.calls.append(call)
        if role == "writer":
            content = case.writes.pop(0) if case.writes else None
            if content is not None:
                (tree / LOCKED).write_text(content)
            message = {"acceptance_files": case.reply_files, "done_when": [LOCKED_CHECK], "questions": [], "notes": []}
        else:
            verdict = case.verdicts.pop(0) if case.verdicts else "pass"
            checked = subprocess.run(LOCKED_CHECK, shell=True, cwd=tree, capture_output=True, text=True)
            message = {"verdict": verdict, "reason": f"{verdict}: (d) the lock misses stderr.",
                       "questions": [], "notes": [],
                       "coverage": [{"statement": statement, "tests": [LOCKED], "break_it": []}
                                    for statement in case.plan["edits"][0]["statements"]],
                       "red": [{"command": LOCKED_CHECK, "collected": 1, "failed": 1,
                                "exit_code": checked.returncode, "output": checked.stdout + checked.stderr or "x",
                                "failure_kind": "assertion"}]}
        log = Path(kw["log_path"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(message))
        return ExecResult(0, False, json.dumps(message), log, session_id=f"{role}-{len(case.calls)}",
                          error_kind="limit" if role == "reviewer" and verdict == "waiting" else "normal")

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


@pytest.mark.parametrize("field", ["checklist", "goal", "out_of_scope", "state"])
def test_other_issue_changes_are_actually_prepared(world, capsys, field):
    case = world
    changes = {"checklist": ["A person reads the notice.", "A person checks the receipt."],
               "goal": GOAL + "\n", "out_of_scope": SCOPE + "\n"}
    _save(case, _edit(**({field: changes[field]} if field != "state" else {})))
    if field == "state":
        case.tracker.issues[NUMBER] = replace(case.tracker.issues[NUMBER], state="closed")
    code, out, err = _cases.invoke(case, capsys, "prepare", str(case.path), "--json")
    assert code == 0, out + err
    assert [call["role"] for call in case.calls] == ["writer", "reviewer"]


@pytest.mark.parametrize("stopped", ["hold", "waiting", "reviewed", "changed_requirements"])
@pytest.mark.parametrize("change_bytes", [False, True])
def test_every_restart_uses_the_current_candidate(world, capsys, stopped, change_bytes):
    case = world
    _save(case, _edit(statements=[CHANGED]))
    case.writes = [REVISED]
    case.verdicts = [{"reviewed": "pass", "changed_requirements": "pass"}.get(stopped, stopped)]
    code, out, err = _cases.invoke(case, capsys, "prepare", str(case.path), "--json")
    assert code == 0, out + err
    assert json.loads(out)["issues"][0]["status"] == (
        "reviewed" if stopped == "changed_requirements" else stopped)
    newer = REVISED + "\n# Current plan candidate.\n"
    plan = json.loads(case.path.read_text())
    if change_bytes:
        plan["edits"][0]["acceptance_files"] = {LOCKED: newer}
    if stopped == "changed_requirements":
        plan["edits"][0]["goal"] += " Check the receipt too."
    case.path.write_text(json.dumps(plan))
    case.plan = plan
    case.verdicts = ["pass"]
    before = len(case.calls)
    code, out, err = _cases.invoke(case, capsys, "prepare", str(case.path), "--json")
    assert code == 0, out + err
    row = json.loads(out)["issues"][0]
    assert row["status"] == "reviewed", row
    expected = newer if change_bytes else REVISED
    assert all(call["seen"] == expected for call in case.calls[before:]), case.calls
    if change_bytes:
        assert case.calls[before:], "Changed candidate bytes need a fresh review"
    assert json.loads(case.path.read_text())["edits"][0]["acceptance_files"] == {LOCKED: expected}
    assert _origin(case, LOCKED) == LOCKED_TEST


@pytest.mark.parametrize("stopped", ["hold", "waiting", "pass"])
def test_current_plan_bytes_also_supply_new_files(world, capsys, stopped):
    case = world
    _save(case, _edit(statements=[CHANGED], acceptance_files={LOCKED: CORRECTED, HELPER: "VALUE = 1\n"}))
    case.reply_files = [LOCKED, HELPER]
    case.verdicts = [stopped]
    code, out, err = _cases.invoke(case, capsys, "prepare", str(case.path), "--json")
    assert code == 0, out + err
    assert case.calls[0]["helper"] == "VALUE = 1\n"
    plan = json.loads(case.path.read_text())
    plan["edits"][0]["acceptance_files"][HELPER] = "VALUE = 2\n"
    case.path.write_text(json.dumps(plan))
    case.plan = plan
    case.verdicts = ["pass"]
    before = len(case.calls)
    code, out, err = _cases.invoke(case, capsys, "prepare", str(case.path), "--json")
    assert code == 0, out + err
    assert json.loads(out)["issues"][0]["status"] == "reviewed"
    assert case.calls[before:] and all(call["helper"] == "VALUE = 2\n" for call in case.calls[before:])
    assert json.loads(case.path.read_text())["edits"][0]["acceptance_files"][HELPER] == "VALUE = 2\n"


def test_a_candidate_edit_during_review_is_not_overwritten_by_promotion(world, capsys, monkeypatch):
    from foragent import order_prepare_review

    case = world
    _save(case, _edit(statements=[CHANGED]))
    review = order_prepare_review.review
    newer = REVISED + "\n# Concurrent plan candidate.\n"

    def edit_during_review(*args, **kwargs):
        review(*args, **kwargs)
        plan = json.loads(case.path.read_text())
        plan["edits"][0]["acceptance_files"] = {LOCKED: newer}
        case.path.write_text(json.dumps(plan))

    monkeypatch.setattr(order_prepare_review, "review", edit_during_review)
    code, out, err = _cases.invoke(case, capsys, "prepare", str(case.path), "--json")
    assert code == 0, out + err
    assert json.loads(out)["issues"][0]["status"] == "interrupted"
    assert json.loads(case.path.read_text())["edits"][0]["acceptance_files"] == {LOCKED: newer}


# Corrected locked files beside the issue's own Done when (r1210) ------------------------------


@pytest.mark.parametrize("on_base, expected", [({HELPER}, [HELPER]), (set(), None)], ids=["existing", "new"])
def test_a_corrected_file_beside_the_locks_is_lock_only_only_when_it_exists_on_the_base(on_base, expected):
    item = _edit(acceptance_files={HELPER: "VALUE = 2\n"})
    view = locks.facts(item, _body(), "tests/acceptance/", repo="owner/repo", base="main",
                       existing=lambda name: name in on_base)
    assert view.get("lock_only") is (True if expected else None)
    assert view.get("beside") == expected


def test_a_corrected_file_beside_the_locks_needs_the_base_to_count():
    """Without a base to look at (no existence check), the edit takes the preparation route."""
    assert not _facts(_edit(acceptance_files={HELPER: "VALUE = 2\n"}), _body()).get("lock_only")
