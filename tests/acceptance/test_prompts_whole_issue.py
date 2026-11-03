"""Acceptance test for prompts-whole-issue. Locked by Foragent; do not edit.

Observe real run_issue calls at the fake executor boundary. Prompt builders,
issue parsing, approval loading, retries and session selection remain real.
"""

from dataclasses import asdict
import shutil
import socket

import pytest

from conftest import GitHistory, git_command
from foragent.executors import ExecResult
from foragent.issue import body_hash
from foragent.runner import run_issue
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home  # noqa: F401


CHECKLIST = "The exported receipt retains the user's selected workspace."
CHECKED = "The receipt preserves the original display name."
CONTRACT = "The export consumer receives the selected workspace identifier."
STABLE_CONTRACT = "The export consumer keeps its existing receipt schema."
CHANGED_CHECKLIST = "The exported receipt retains the reapproved workspace identifier."
CHANGED_CONTRACT = "The export consumer receives the reapproved workspace identifier."
NOTE = "An interrupted export retains the pending local receipt."
WAITING = "Gate bookkeeping only: waiting for the publication boundary."
PRIOR_WAITING = "Gate bookkeeping only: the previous publication boundary."
SESSION = "offline-whole-issue-session"
FINDING = "VERDICT: FAIL\nfeature.txt:1 — the export loses its selected workspace"


@pytest.fixture(autouse=True)
def offline(monkeypatch, isolated_home):
    from foragent import executors

    def forbidden(*args, **kwargs):
        pytest.fail("Whole-issue acceptance uses fake executors and stays offline")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(executors, "run_logged", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)


@pytest.fixture
def cases(git_history, tmp_path, monkeypatch):
    def create(name):
        directory = tmp_path / name
        directory.mkdir()
        origin = shutil.copytree(git_history.origin, directory / "origin.git")
        clone = shutil.copytree(git_history.clone, directory / "clone")
        git_command(clone, "config", "remote.origin.url", str(origin))
        history = GitHistory(origin, clone, git_history.tip)
        return make_case(history, directory, monkeypatch, executor="claude", reviewer="codex",
                         skill_libraries=[], test_command="true", review_max=1,
                         max_retries=1, language="en")

    return create


def approved_body(case, *, long=False):
    base = case.store.load_approval(1).body
    padding = "".join(f"Authored detail {number}: retain the selected workspace and receipt.\n"
                      for number in range(1100)) if long else ""
    body = (
        base + f"- [ ] {CHECKLIST}\n- [x] {CHECKED}\n"
        f"\n## Waiting\n{WAITING}\n"
        "<!-- foragent:waiting-prior sha256=" + "a" * 64 + " id=" + "b" * 32 + " -->\n"
        f"\n## Contracts\n{CONTRACT}\n{STABLE_CONTRACT}\n{padding}"
        f"\n## Integration notes\n{NOTE}\n"
        "<!-- foragent:plan-child id=offline-whole-issue key=feature -->\n"
    )
    required = ["Implement the feature.", "Other features.", "test -f feature.txt",
                "test -f tests/acceptance/test_feature.py", f"- [ ] {CHECKLIST}",
                f"- [x] {CHECKED}", f"## Contracts\n{CONTRACT}", STABLE_CONTRACT,
                f"## Integration notes\n{NOTE}"]
    if padding:
        assert len(padding) > 60000
        required.append(padding)
    return body, required


def approve(case, body):
    snapshot = asdict(case.store.load_approval(1))
    case.tracker.update_issue(1, title="Implement feature", body=body)
    case.store.save_approval(**{**snapshot, "body": body, "body_sha": body_hash(body)})


def assert_authored(prompt, required):
    missing = [text[:110] for text in required if text not in prompt]
    forbidden = [text for text in (WAITING, PRIOR_WAITING, "## Waiting",
                                   "<!-- foragent:", "<!-- /foragent:")
                 if text in prompt]
    assert not missing and not forbidden, {"missing_authored_text": missing,
                                          "leaked_gate_text": forbidden}


def assert_pr_checklist(case, unchecked=CHECKLIST):
    body = case.tracker.created_prs[0].body
    assert "## Needs a human check" in body
    assert f"- [ ] {unchecked}" in body and f"- [ ] {CHECKED}" in body


@pytest.mark.parametrize("long", [False, True], ids=["short", "over-60000-chars"])
def test_first_attempt_receives_authored_issue_and_ordinary_issue_still_runs(cases, long):
    # Keep the already-green control inside this regression: every new node is red today.
    ordinary = cases("ordinary")
    assert run_issue(1, ordinary.ctx) == "merged"
    assert len(ordinary.implementer.calls) == len(ordinary.reviewer.calls) == 1
    for call in (ordinary.implementer.calls[0], ordinary.reviewer.calls[0]):
        for text in ("Implement the feature.", "Other features.", "test -f feature.txt",
                     "test -f tests/acceptance/test_feature.py"):
            assert text in call["prompt"]
        assert CHECKLIST not in call["prompt"] and "## Contracts" not in call["prompt"]
    assert "## Needs a human check" not in ordinary.tracker.created_prs[0].body

    case = cases("authored")
    body, required = approved_body(case, long=long)
    approve(case, body)
    # Live unapproved changes must not replace the approved requirements.
    case.tracker.update_issue(1, title="Implement feature", body="UNAPPROVED LIVE TEXT")
    assert run_issue(1, case.ctx) == "merged"
    call, = case.implementer.calls
    assert not call.get("resume_session_id")
    assert_pr_checklist(case)
    assert "UNAPPROVED LIVE TEXT" not in call["prompt"]
    assert_authored(call["prompt"], required)


@pytest.mark.parametrize("mode", ["fresh-retry", "changed-authored-resume",
                                  "changed-authored-and-gate-resume"])
def test_retry_gets_the_whole_issue_and_resume_gets_only_authored_changes(cases, mode):
    case = cases(mode)
    body, required = approved_body(case, long=True)
    approve(case, body)

    if mode == "fresh-retry":
        def execute(**kwargs):
            case.implementer.calls.append(kwargs)
            case.implement(kwargs)
            return ExecResult(0, False, "Implemented", kwargs["log_path"])

        case.implementer.run = execute
        case.reviewer.steps = [FINDING, "VERDICT: PASS"]
    else:
        def execute(**kwargs):
            case.implementer.calls.append(kwargs)
            if len(case.implementer.calls) == 1:
                question = kwargs["worktree"] / ".foragent/QUESTION.md"
                question.parent.mkdir(exist_ok=True)
                question.write_text("Which export requirements should apply?\n", encoding="utf-8")
            else:
                case.implement(kwargs)
            return ExecResult(0, False, "Implementation paused or completed", kwargs["log_path"],
                              session_id=SESSION)

        case.implementer.run = execute
        assert run_issue(1, case.ctx) == "spec-question"
        assert not case.reviewer.calls
        revised = body.replace(CHECKLIST, CHANGED_CHECKLIST).replace(CONTRACT, CHANGED_CONTRACT)
        if mode == "changed-authored-and-gate-resume":
            revised = revised.replace(WAITING, PRIOR_WAITING)
            revised = revised.replace("id=" + "b" * 32, "id=" + "c" * 32)
        approve(case, revised)
        from foragent import orch

        orch.retry(case.ctx, 1, hint="Use the reapproved requirements and finish.")
        case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))

    assert run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 2
    assert not case.implementer.calls[0].get("resume_session_id")
    call = case.implementer.calls[-1]
    assert call.get("resume_session_id") == (None if mode == "fresh-retry" else SESSION)
    if mode == "fresh-retry":
        assert "the export loses its selected workspace" in call["prompt"]
        assert_pr_checklist(case)
        assert_authored(call["prompt"], required)
    else:
        prompt = call["prompt"]
        assert f"- [ ] {CHANGED_CHECKLIST}" in prompt
        assert CHANGED_CONTRACT in prompt
        unchanged = ["Implement the feature.", "Other features.", "test -f feature.txt",
                     "test -f tests/acceptance/test_feature.py", CHECKED, STABLE_CONTRACT, NOTE,
                     *(f"Authored detail {number}: retain the selected workspace and receipt."
                       for number in range(1100))]
        repeated = [text[:110] for text in unchanged if text in prompt]
        assert not repeated, {"unchanged_issue_text_repeated": repeated}
        assert_pr_checklist(case, unchecked=CHANGED_CHECKLIST)
        # Delta delivery is already supported. It depends on the first prompt
        # having supplied the unchanged requirements, which today's runner loses.
        assert_authored(case.implementer.calls[0]["prompt"], required)
        assert_authored(prompt, [f"- [ ] {CHANGED_CHECKLIST}", CHANGED_CONTRACT])


@pytest.mark.parametrize("long", [False, True], ids=["short", "over-60000-chars"])
def test_reviewer_receives_all_authored_issue_content(cases, long):
    case = cases("review")
    body, required = approved_body(case, long=long)
    approve(case, body)
    assert run_issue(1, case.ctx) == "merged"
    call, = case.reviewer.calls
    assert "Diff:" in call["prompt"]
    assert_pr_checklist(case)
    assert_authored(call["prompt"], required)
