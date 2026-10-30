"""Acceptance test for night-preread-notes. Locked by Foragent; do not edit."""

import pytest

from test_order import plan, publishing
from test_order_preread import UNKNOWN, assert_published, preread, preread_case, publish
from test_skills_autosync import isolated_home


@pytest.mark.parametrize("tag", ["APPROACH: Keep the existing interface.", "DECIDE: Use the existing catalog."])
def test_structured_reply_notes_before_and_after_tags_never_block_approval(preread_case, capsys, tag):
    case = preread_case
    message = f"Notes about the plan.\n{tag}\n- Further implementation notes."
    payload = preread(case, capsys, message, "NONE")
    assert [i["unknowns"] for i in payload["issues"]] == [[], []], payload
    if tag.startswith("DECIDE:"):
        assert payload["issues"][0]["decisions"] == ["Use the existing catalog."]
    publish(case, capsys)
    assert_published(case)


def test_only_question_lines_block_a_structured_reply(preread_case, capsys):
    case = preread_case
    reply = f"Notes before the question.\n1. QUESTION: {UNKNOWN}\nNotes after the question."
    payload = preread(case, capsys, reply, "NONE")
    assert [i["unknowns"] for i in payload["issues"]] == [[UNKNOWN], []], payload
    publish(case, capsys, blocked_key="api", unknowns=True)
