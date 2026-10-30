"""D98: actual reviewer instructions judge the goal, not implementation taste."""

import re

import pytest

from foragent.issue import parse_body
from foragent.run_prompts import build_review_prompt
from test_order_lane_proposal import APPROACH, CHOICE, OTHER_CHOICE, run_proposed_issue
from test_orch_review_handoff import make_case


def test_public_review_prompt_defines_failure_and_accepts_valid_alternative_approaches():
    # This public prompt builder is an explicitly named contract of issue 3.
    parsed = parse_body("## Goal\nKeep confirmation behaviour.\n## Out of scope\nPayments.\n"
                        "## Done when\n```sh\ntest -f feature.txt\n```\n")
    text = " ".join(build_review_prompt(parsed, "diff --git a/feature.txt b/feature.txt").lower().split())
    assert re.search(r"fail only|only.{0,100}(?:fail|block)", text)
    assert re.search(r"locked.{0,60}test.{0,100}(?:fail|circumvent)|"
                     r"(?:fail|circumvent).{0,100}locked.{0,60}test", text)
    assert re.search(r"principle.{0,80}(?:broken|violat)|(?:broken|violat).{0,80}principle", text)
    assert all(word in text for word in ("bug", "race", "data loss", "security"))
    assert re.search(r"different.{0,50}valid.{0,80}approach.{0,160}(?:not|never).{0,50}"
                     r"(?:fail|defect|block)", text)
    assert re.search(r"(?:naming|names).{0,50}structure.{0,100}(?:not|never).{0,50}"
                     r"(?:fail|defect|block)", text)
    assert "verdict: pass" in text and "verdict: fail" in text
    assert re.search(r"note.{0,140}never.{0,40}(?:reason to fail|fail|block)", text)


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
def test_the_actual_reviewer_receives_the_lanes_recorded_choices(git_history, tmp_path, monkeypatch, capsys, reviewer):
    case = run_proposed_issue(git_history, tmp_path, monkeypatch, capsys, reviewer=reviewer)
    prompt = case.review_inputs[0]
    assert CHOICE in prompt and OTHER_CHOICE in prompt
    # The choices arrive as context. They do not acquire the force of locked tests.
    flat = " ".join(prompt.lower().split())
    assert re.search(r"(?:decide|lane.{0,20}(?:choice|decision)).{0,120}context|"
                     r"context.{0,120}(?:decide|lane.{0,20}(?:choice|decision))", flat)


@pytest.mark.parametrize("reply,outcome", [
    ("VERDICT: PASS\nNOTE: feature.txt:1 a different structure would also be valid", "merged"),
    ("VERDICT: FAIL\nfeature.txt:1 loses the person's confirmation", "held"),
])
def test_notes_never_fail_but_an_actual_defect_still_fails(git_history, tmp_path, monkeypatch, reply, outcome):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch, review_max=1, max_retries=1,
                     skill_libraries=[], test_command="true")
    case.reviewer.steps = [reply, reply] if outcome == "held" else [reply]
    if outcome == "held":
        case.implementer.steps = [case.implement, case.implement]
    assert run_issue(1, case.ctx) == outcome
