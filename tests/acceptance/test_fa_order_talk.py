"""Acceptance test for fa-order-talk. Locked by Foragent; do not edit."""

from pathlib import Path

from test_orchestrator_publishes_choice_free_fixes import assert_choice_free_boundary

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / ".foragent.yml").is_file())
import re


def talk_text():
    path = ROOT / "skills/fa-order/SKILL.md"
    return " ".join(path.read_text(encoding="utf-8").casefold().split())


def test_fa_order_talk_keeps_internal_records_out_of_conversation_unless_requested():
    path = ROOT / "skills/fa-order/SKILL.md"
    assert re.search(r"^## Talking to the person$", path.read_text(encoding="utf-8"), re.M)
    text = talk_text()
    assert re.search(
        r"(?:record|store).{0,30}decision ledger.{0,30}chunk cells.{0,30}source labels"
        r".{0,50}fa order progress.{0,40}these records.{0,20}not shown"
        r".{0,30}person.{0,20}as lists.{0,20}(?:unless|except when).{0,30}person asks",
        text,
    ), "Record the ledger, chunk cells and source labels; show lists only on request"


def test_fa_order_talk_puts_the_persons_problem_and_outcome_before_one_decision():
    text = talk_text()
    assert re.search(
        r"every question (?:or|and) confirmation.{0,30}person.{0,30}(?:starts|begins)"
        r".{0,40}in (?:this )?order.{0,30}problem.{0,30}person['’]s own words"
        r".{0,70}(?:then|next).{0,20}what will change.{0,30}person"
        r".{0,30}(?:work is done|work is finished).{0,20}(?:then|finally)"
        r".{0,20}one decision to make",
        text,
    ), "Every question and confirmation needs problem, personal outcome, then one decision"


def test_fa_order_talk_quotes_only_actual_words_and_labels_the_sessions_summary():
    text = talk_text()
    assert re.search(
        r"only words.{0,20}person.{0,20}actually said.{0,30}quotation marks"
        r".{0,20}(?:or|and).{0,20}attributed to them",
        text,
    ), "Quotes and attribution must use words the person actually said"
    assert re.search(
        r"(?:mark|label).{0,20}summary.{0,20}agreement.{0,20}as"
        r".{0,20}session['’]s summary",
        text,
    ), "The session must identify its own summary of the agreement"


def test_fa_order_talk_confirms_scope_once_and_settles_reversible_details_itself():
    text = talk_text()
    assert re.search(
        r"confirm.{0,20}scope.{0,20}(?:one|single) question"
        r".{0,20}is this what you wanted\?",
        text,
    ), "Confirm scope with one question about what the person wanted"
    assert re.search(
        r"(?:session|agent).{0,20}(?:settles|decides).{0,20}reversible details"
        r".{0,20}itself.{0,20}(?:keeps|records).{0,20}(?:in the records|in the ledger)",
        text,
    ), "The session settles reversible details itself and records them"


def test_fa_order_talk_requires_first_hand_approval_in_the_publishing_session():
    """Historical name retained: first-hand person approval applies to D71 choices;
    choice-free fixes are published and approved by the orchestrator after clean preread.
    """
    text = talk_text()
    assert_choice_free_boundary(text, "fa-order")
    assert re.search(r"first.hand.{0,100}person|person.{0,100}first.hand", text)
    assert re.search(r"approval relayed.{0,20}from another session.{0,20}(?:is not|does not count as) approval", text)


def test_fa_order_talk_localizes_confirmation_but_preserves_original_quotes():
    text = talk_text()
    assert re.search(
        r"confirm scope with one question.{0,15}e\.g\..{0,10}is this what you wanted\?"
        r".{0,20}in english.{0,20}ask the confirmation question"
        r".{0,15}in the person['’]s language",
        text,
    ), "The English scope question is an example; ask it in the person's language"
    assert re.search(
        r"keep.{0,20}person['’]s original words.{0,15}untranslated.{0,15}in quotes",
        text,
    ), "Quotes preserve what the person actually said, including their language"


def test_fa_order_talk_exposes_self_settled_decisions_once_before_approval():
    text = talk_text()
    assert re.search(
        r"before approval.{0,15}list the decisions.{0,20}session settled itself"
        r".{0,15}once in this restatement.{0,20}i decided these for you"
        r".{0,80}one short list.{0,20}person can change them",
        text,
    ), "Expose self-settled choices once so the person can correct them before approval"
    assert re.search(
        r"keep the decision ledger.{0,25}source labels.{0,25}chunk cells internal",
        text,
    ), "A short decision summary must not expose internal records"
    assert not re.search(r"what is out of scope\..{0,10}keep reversible details in the records", text)


def test_fa_order_talk_closes_the_originating_request_after_successful_handoff_publication():
    text = talk_text()
    assert re.search(
        r"after this planning session successfully publishes.{0,30}plan the orchestrator drafted"
        r".{0,30}originating request done with.{0,10}fa orch request-status id done"
        r" --note \"<issues>\".{0,100}published issue numbers",
        text,
    ), "Close the originating request with the published issue numbers, after success"
    assert re.search(r"failed publication.{0,20}leaves the request processing", text)
