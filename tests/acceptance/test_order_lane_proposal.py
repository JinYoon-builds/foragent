"""D98: preread proposals, approval and the actual implementation handoff."""

from pathlib import Path
import json
import re

import pytest

from test_order import plan, publishing  # noqa: F401
from test_order_preread import preread_case, preread, publish, assert_published  # noqa: F401
from test_orch_review_handoff import make_case, bind_cli
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


APPROACH = "Keep the public confirmation contract and reuse the existing writer."
CHOICE = "Choose a helper name while implementing the writer."
OTHER_CHOICE = "Use a local tuple for the reversible intermediate result."
REPLY = f"APPROACH: {APPROACH}\nDECIDE: {CHOICE}\nDECIDE: {OTHER_CHOICE}"


def test_a_lane_proposal_is_returned_recorded_and_does_not_block_approval(preread_case, capsys):
    case = preread_case
    payload = preread(case, capsys, REPLY, "NONE")
    row = payload["issues"][0]
    assert row["unknowns"] == [], "A design proposal must not become a list of blockers"
    assert APPROACH in json.dumps(row["approach"])
    for choice in (CHOICE, OTHER_CHOICE):
        assert json.dumps(row["decisions"]).count(choice) == 1
    record = (case.root / ".foragent/preread.json").read_text()
    assert all(text in record for text in (APPROACH, CHOICE, OTHER_CHOICE))
    prompt = " ".join(case.worker.calls[0]["prompt"].lower().split())
    assert "your lane owns how" in prompt
    assert all(tag in prompt for tag in ("approach", "decide:", "question:"))
    assert all(word in prompt for word in ("locked test", "contract", "behaviour"))
    publish(case, capsys)
    assert_published(case)


@pytest.mark.parametrize("question", [
    "QUESTION: Which locale must the person see?",
    "The locale contract is missing.",
])
def test_only_questions_and_legacy_plain_lines_block_then_a_new_proposal_clears_them(
    preread_case, capsys, question,
):
    case = preread_case
    row = preread(case, capsys, REPLY + "\n" + question, "NONE")["issues"][0]
    assert len(row["unknowns"]) == 1
    assert question.removeprefix("QUESTION: ") in row["unknowns"][0]
    assert CHOICE in json.dumps(row["decisions"])
    assert APPROACH in json.dumps(row["approach"])
    publish(case, capsys, blocked_key="api", unknowns=True)
    row = preread(case, capsys, REPLY, "NONE")["issues"][0]
    assert row["unknowns"] == []
    publish(case, capsys)
    assert_published(case)


def run_proposed_issue(git_history, tmp_path, monkeypatch, capsys, *, replies=(REPLY,), reviewer="codex"):
    """Drive real CLI preread/publication and run_issue with offline service adapters.

    No preread parser, record loader or prompt builder is substituted. Git commands
    stay in the fixture's temporary histories; commit and push are emulated there.
    """
    from conftest import ACCEPTANCE
    from foragent import cli, executors, order
    from foragent.issue import body_hash, parse_body
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch,
                     executor="codex" if reviewer == "claude" else "claude", reviewer=reviewer,
                     skill_libraries=[], test_command="true")
    main = bind_cli(case, monkeypatch)
    parsed = parse_body(case.tracker.get_issue(1).body)
    item = {"key": "feature", "title": "Keep the confirmation contract",
            "lane": "main", "size": "m", "goal": parsed.goal,
            "out_of_scope": parsed.out_of_scope, "done_when": parsed.done_when_commands,
            "files": ["feature.txt"], "blocked_by": [], "acceptance_files": {}}
    plan = {"id": "d98-proposal-handoff", "title": "Confirmation writer",
            "summary": "Keep the existing confirmation behaviour.",
            "spec_markdown": "# Confirmation writer\nNo additional product behaviour.\n",
            "issues": [item]}
    path = case.root / "PLAN.json"
    path.write_text(json.dumps(plan))
    monkeypatch.setattr(executors, "executor_for", lambda name: case.implementer)
    for module in (cli, order):
        if hasattr(module, "executor_for"):
            monkeypatch.setattr(module, "executor_for", lambda name: case.implementer)
    for reply in replies:
        case.implementer.steps = [reply]
        capsys.readouterr()
        assert cli_exit(main, ["order", "preread", str(path), "--json"]) == 0
        json.loads(capsys.readouterr().out)
    # Publish without approval, then use the normal CLI approval gate. This also
    # permits observing the missing handoff on main before the blocker is fixed.
    with monkeypatch.context() as publisher:
        # Publication is allowed to add approval; runner operations remain strict.
        publisher.setattr(case.tracker, "strict", False)
        publisher.setattr(case.tracker, "actor", "human")
        assert cli_exit(main, ["order", "publish", str(path)]) == 0
        child = next(issue for issue in case.tracker.list_issues()
                     if issue.title == item["title"])
        assert cli_exit(main, ["order", "publish", str(path), "--approve"]) == 0
    body = case.tracker.get_issue(child.number).body
    case.store.save_approval(child.number, body=body, body_sha=body_hash(body),
                             acceptance_blobs=case.git.blob_shas("origin/main", [ACCEPTANCE]),
                             approved_by="human", approved_at=case.clock[0].isoformat())
    case.store.start_run(child.number, branch=f"foragent/issue-{child.number}",
                         worktree=case.store.worktree_path(child.number))
    case.implementer.calls.clear()
    case.implementer.steps = [case.implement]
    case.review_inputs = []

    def review(kwargs):
        scope = kwargs["prompt"]
        if reviewer == "claude":
            scope += (kwargs["worktree"] / "CLAUDE.local.md").read_text()
        case.review_inputs.append(scope)
        return json.dumps({"result": "(none)"}) if reviewer == "claude" else "VERDICT: PASS"

    case.reviewer.steps = [review]
    assert run_issue(child.number, case.ctx) == "merged"
    return case


def test_the_run_receives_the_lanes_latest_earlier_plan(git_history, tmp_path, monkeypatch, capsys):
    old = "APPROACH: Obsolete proposal.\nDECIDE: Obsolete helper choice."
    case = run_proposed_issue(git_history, tmp_path, monkeypatch, capsys, replies=(old, REPLY))
    prompt = case.implementer.calls[0]["prompt"]
    assert "Your lane's earlier plan" in prompt
    assert all(text in prompt for text in (APPROACH, CHOICE, OTHER_CHOICE))
    assert "Obsolete proposal" not in prompt and "Obsolete helper choice" not in prompt


def test_d98_records_the_dated_decision_and_what_it_supersedes():
    text = (Path(__file__).resolve().parents[2] / "docs/decisions.md").read_text()
    section = re.search(r"^### D98\.[^\n]*2026-10-30[^\n]*\n(.*?)(?=^### |\Z)",
                        text, re.M | re.S)
    assert section, "Issue 1 must append the Korean D98 decision dated 2026-10-30"
    entry = section[0]
    assert "WHAT" in entry and "HOW" in entry
    assert re.search(r"[가-힣]", entry)
    assert "fa-order" in entry and "10-27" in entry
    assert re.search(r"대체|supersed", entry, re.I)
