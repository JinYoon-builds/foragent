"""Acceptance test for orch-order-handoff. Locked by Foragent; do not edit."""

from pathlib import Path

from test_orchestrator_publishes_choice_free_fixes import assert_choice_free_boundary

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / ".foragent.yml").is_file())
import re

import pytest


@pytest.fixture(params=["SKILL.md", "references/order-card.md"])
def handoff_text(request):
    root = ROOT / "skills/fa-orchestrator"
    text = (root / request.param).read_text(encoding="utf-8")
    if request.param == "SKILL.md":
        pointer = re.search(r"\[order card\]\((references/order-card\.md)\)", text)
        if pointer:
            text = (root / pointer[1]).read_text(encoding="utf-8")
    return " ".join(text.casefold().split())


def test_orch_order_handoff_entrypoint_is_a_one_line_pointer_within_the_skill_limit():
    root = ROOT / "skills/fa-orchestrator"
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    assert len(skill.splitlines()) <= 150
    assert re.search(
        r"^For order drafts[^\n]{0,60}fa-order[^\n]{0,60}\[order card\]"
        r"\(references/order-card\.md\)[^\n]{0,80}planning handoff"
        r"[^\n]{0,80}fa order publish --approve[^\n]*$",
        skill, re.M,
    ), "Route handoff through one line in SKILL.md and maintain its existing size limit"
    assert "fa orch ask --kind order --question" not in skill, "Put handoff detail in the reference"


def test_orch_order_handoff_records_the_plan_json_path_in_the_order_question(handoff_text):
    assert re.search(
        r"when the orchestrator drafts.{0,15}plan.{0,25}write.{0,20}plan json path"
        r".{0,25}content hash.{0,20}into the order question.{0,30}fa orch ask --kind order"
        r".{0,25}--question.{0,50}plan json path: <actual state drafts path>"
        r"; plan_hash: <content hash>; originating request: id",
        handoff_text,
    ), "An orchestrator draft must put the actual plan JSON path in fa orch ask --kind order"


def test_orch_order_handoff_planning_asks_the_person_and_publishes_after_approval_there(handoff_text):
    assert re.search(
        r"send.{0,20}question and plan path.{0,20}planning session"
        r".{0,30}(?:that|planning) session asks the person"
        r".{0,20}only after approval there.{0,20}publishes with"
        r".{0,15}fa order publish --approve plan\.json",
        handoff_text,
    ), "Send the question and plan to planning, which asks and publishes after approval there"


def test_orch_order_handoff_orchestrator_publishes_only_after_approval_in_its_own_session(handoff_text):
    """Historical name retained: D71 choices need the person's planning-session
    approval; a choice-free fix instead needs the orchestrator's clean preread.
    """
    assert_choice_free_boundary(handoff_text, "orchestrator order-card guidance")


def test_orch_order_handoff_relayed_add_is_not_publication_approval(handoff_text):
    assert re.search(
        r"approval relayed.{0,20}from another session.{0,20}is not approval",
        handoff_text,
    ), "A recorded answer relayed from another session must not authorize publication"
    assert not re.search(
        r"after the user chose \[add\].{0,20}publish with",
        handoff_text,
    ), "Remove the unconditional publish-after-add instruction"


def test_orch_order_handoff_keeps_the_plan_in_state_drafts_with_a_current_content_hash(handoff_text):
    assert re.search(
        r"store the plan json in.{0,10}~/.local/share/foragent/<repo>/drafts/<order-id>/plan\.json"
        r".{0,15}never /tmp or a worktree",
        handoff_text,
    ), "The handed-off plan must survive a worktree or temporary folder disappearing"
    assert re.search(
        r"current content['’]s.{0,10}plan_hash.{0,15}from preread/perspectives"
        r".{0,30}file['’]s sha256.{0,20}refresh the hash after any plan edit",
        handoff_text,
    ), "Identify current plan contents with an existing plan_hash or the file's sha256"


def test_orch_order_handoff_explains_the_existing_push_when_planning_is_stale(handoff_text):
    assert re.search(
        r"when the planning heartbeat is stale.{0,20}fa orch ask.{0,20}already pushes"
        r".{0,30}to the person.{0,20}#970.{0,100}closed planning session"
        r".{0,20}not hide the plan",
        handoff_text,
    ), "The existing stale-heartbeat push keeps the durable plan visible when planning is closed"


def test_orch_order_handoff_preserves_and_completes_the_originating_request(handoff_text):
    assert re.search(
        r"keep the originating request id with the draft.{0,60}close that request",
        handoff_text,
    )
    assert re.search(
        r"after the planning session successfully publishes the orchestrator.drafted plan"
        r".{0,30}originating request done with.{0,10}fa orch request-status id done"
        r" --note \"<issues>\".{0,120}published issue numbers",
        handoff_text,
    ), "Only successful publication completes the originating request, using published issues"
    assert re.search(r"failed publication.{0,20}leaves the request processing", handoff_text)
