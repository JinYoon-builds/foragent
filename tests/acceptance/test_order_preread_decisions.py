"""Acceptance test for order-preread-decisions. Locked by Foragent; do not edit."""

import pytest

from foragent import order
from test_order import plan, publishing  # noqa: F401
from test_order_preread import UNKNOWN, assert_published, preread, preread_case, publish  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


DECIDED = "DECIDE: The confirmation uses the existing en-US message catalog."
APPROACH = "APPROACH: Add the confirmation beside the existing client flow."


def test_preread_prompt_gives_the_lane_ownership_of_how():
    plan = {"title": "T", "summary": "", "spec_markdown": "", "issues": [
        {"key": "a", "title": "A", "lane": "api", "goal": "g", "out_of_scope": "o", "done_when": ["true"]}]}
    prompt = order.preread_prompt(plan, plan["issues"][0])
    assert "DECIDE:" in prompt and "QUESTION:" in prompt and "APPROACH:" in prompt
    assert "lane owns" in prompt.lower()


@pytest.mark.parametrize("preread_case", ["codex"], indirect=True)
def test_decide_and_approach_lines_do_not_block_approval(preread_case, capsys):
    case = preread_case
    payload = preread(case, capsys, f"{APPROACH}\n{DECIDED}", DECIDED)
    for item in payload["issues"]:
        assert item["unknowns"] == [], item
        assert item["decisions"] == ["The confirmation uses the existing en-US message catalog."], item
    publish(case, capsys)
    assert_published(case)


@pytest.mark.parametrize("preread_case", ["codex"], indirect=True)
def test_question_lines_still_block_approval(preread_case, capsys):
    case = preread_case
    payload = preread(case, capsys, DECIDED, f"{DECIDED}\nQUESTION: {UNKNOWN}")
    by_key = {item["key"]: item for item in payload["issues"]}
    assert [item["unknowns"] for item in payload["issues"]].count([]) == 1
    blocked = next(key for key, item in by_key.items() if item["unknowns"])
    assert by_key[blocked]["unknowns"] == [UNKNOWN]
    publish(case, capsys, blocked_key=blocked, unknowns=True)


@pytest.mark.parametrize("preread_case", ["codex"], indirect=True)
def test_unprefixed_lines_keep_counting_as_unknowns(preread_case, capsys):
    case = preread_case
    payload = preread(case, capsys, "NONE", UNKNOWN)
    assert [item["unknowns"] for item in payload["issues"]] == [[], [UNKNOWN]]
