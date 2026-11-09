"""Acceptance tests for follow-up-promise (architect r1283, order 2026-11-09-followups).

A 'ship with follow-up' answer to the gate's review-decision request for issue N carries the
follow-up text. Answering that way records one open orchestrator promise on N that names the
text and becomes due when N merges (``merged:N``), so the existing promise checks raise it:
the gate marks it due (one ``promise-due`` request to the orchestrator), the orchestrator's
next prompt lists it, and it stays due (or late) until ``fa orch promise done`` closes it.

Public contract these tests read:

- The gate asks with one orchestrator inbox request ``kind: review-decision, issue: N``
  (run_review._ask_review_decision builds it with ``orch_inbox.add_request(store, "gate", ...)``;
  the helper below makes the same call).
- The orchestrator answers with ``fa orch request-status ID done --answer
  "ship with follow-up: <follow-up text>"``: the follow-up text rides in the answer after the
  releasing words, which is how answers already carry it today.
- Promises are read through ``fa orch promise list --json`` (``id``, ``when``, ``do``,
  ``owner``, ``state``) and the orchestrator's prompt section
  (``orch_promise.prompt_section(store, "orchestrator")``); due notices through
  ``fa orch inbox --json``.

Every check runs the real ``fa`` CLI with an offline FakeTracker and real ``fa gate tick`` runs
(tick_case). No process is started and no signal is sent.
"""

from __future__ import annotations

from shelf_object_support import requests
from test_promises_shelf import happen, listed, open_issue, run, state, tick
from test_skills_autosync import isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import tick_case  # noqa: F401 (fixture)


HELD = 2741
OTHER = 2742
FOLLOW_UP = "add a retry test for the cache eviction path heron-7Q"
OTHER_FOLLOW_UP = "split the parser error messages into one table walrus-3K"


def ask_review_decision(case, n):
    """The gate's review-decision request for ``n``, made the way run_review makes it."""
    from foragent import orch_inbox, review_scoring

    text = (f"Review hold for #{n}: two failed review rounds share the defect class result persistence. "
            f"Answer 'ship with follow-up' or 'narrow' with `fa orch request-status ID done --answer`; "
            f"escalate to the PM only for a product-behaviour choice.\n{review_scoring.REVIEW_RULE}")
    record = orch_inbox.add_request(case.store, "gate", text, {
        "kind": review_scoring.REQUEST_KIND, "issue": n, "repeated_classes": ["result persistence"],
        "rounds": [{"round": 1, "finding_classes": ["result persistence"]},
                   {"round": 2, "finding_classes": ["result persistence"]}]})
    return record["id"]


def answer(case, capsys, request_id, text):
    code, out, err = run(case, capsys, "orch", "request-status", str(request_id), "done", "--answer", text)
    assert code == 0, f"fa orch request-status {request_id} done --answer must succeed ({code}): {err}{out}"


def follow_up_promises(case, capsys, text):
    return [row for row in listed(case, capsys).values() if text in str(row.get("do", ""))]


def the_promise(case, capsys, text):
    rows = follow_up_promises(case, capsys, text)
    assert len(rows) == 1, (
        f"The 'ship with follow-up' answer records exactly one promise naming {text!r}: {listed(case, capsys)}")
    return rows[0]


def due_notices(case, capsys, promise_id):
    return [row for row in requests(case, capsys)
            if row.get("kind") == "promise-due" and str(row.get("promise")) == str(promise_id)]


def held_world(case, capsys):
    open_issue(case, HELD)
    open_issue(case, OTHER)
    tick(case, capsys)  # The gate observes both issues before anyone answers.
    return ask_review_decision(case, HELD)


# ---------------------------------------------------------------- statement 1


def test_ship_with_follow_up_answer_records_one_orchestrator_promise_due_when_the_issue_merges(tick_case, capsys):
    case = tick_case
    request_id = held_world(case, capsys)
    assert listed(case, capsys) == {}, "No promise exists before the answer"

    answer(case, capsys, request_id, f"ship with follow-up: {FOLLOW_UP}")

    rows = follow_up_promises(case, capsys, FOLLOW_UP)
    assert len(rows) == 1, (
        f"The answer records exactly one promise naming its follow-up text {FOLLOW_UP!r}: "
        f"{listed(case, capsys)}")
    (row,) = rows
    assert row["owner"] == "orchestrator", f"The follow-up is the orchestrator's promise: {row}"
    assert row["when"] == f"merged:{HELD}", f"The promise is on #{HELD} and becomes due when it merges: {row}"
    assert row["state"] == "waiting", f"Before #{HELD} merges the promise waits: {row}"
    assert len(listed(case, capsys)) == 1, f"One answer records one promise, not more: {listed(case, capsys)}"

    for _ in range(2):
        tick(case, capsys)
    assert len(listed(case, capsys)) == 1, "Later ticks keep the one promise, not doubled"
    assert state(case, capsys, row["id"]) == "waiting", f"#{HELD} has not merged; the promise still waits"
    assert due_notices(case, capsys, row["id"]) == [], "Nobody is told before the merge"


def test_each_review_answer_records_its_own_promise_on_its_own_issue(tick_case, capsys):
    case = tick_case
    first = held_world(case, capsys)
    second = ask_review_decision(case, OTHER)

    answer(case, capsys, first, f"ship with follow-up: {FOLLOW_UP}")
    answer(case, capsys, second, f"Ship with follow-up: {OTHER_FOLLOW_UP}")

    shown = listed(case, capsys)
    assert len(shown) == 2, f"Two answers record two promises: {shown}"
    by_text = {text: follow_up_promises(case, capsys, text) for text in (FOLLOW_UP, OTHER_FOLLOW_UP)}
    assert [row["when"] for row in by_text[FOLLOW_UP]] == [f"merged:{HELD}"], by_text
    assert [row["when"] for row in by_text[OTHER_FOLLOW_UP]] == [f"merged:{OTHER}"], by_text
    assert {row["owner"] for rows in by_text.values() for row in rows} == {"orchestrator"}, by_text


def test_the_promise_survives_a_reopened_store(tick_case, capsys):
    from foragent import orch_promise
    from foragent.state import StateStore

    case = tick_case
    request_id = held_world(case, capsys)
    answer(case, capsys, request_id, f"ship with follow-up: {FOLLOW_UP}")

    reopened = [row for row in orch_promise.promises(StateStore(case.store.root))
                if FOLLOW_UP in str(row.get("do", ""))]
    assert len(reopened) == 1, "The promise is durable state, not only an event summary"
    assert reopened[0]["owner"] == "orchestrator" and reopened[0]["when"] == f"merged:{HELD}", reopened


# ---------------------------------------------------------------- statement 2


def test_after_the_merge_the_promise_is_due_to_the_orchestrator_until_it_is_kept(tick_case, capsys):
    from foragent import orch_promise

    case = tick_case
    request_id = held_world(case, capsys)
    answer(case, capsys, request_id, f"ship with follow-up: {FOLLOW_UP}")
    row = the_promise(case, capsys, FOLLOW_UP)
    assert FOLLOW_UP not in orch_promise.prompt_section(case.store, "orchestrator"), (
        "Before the merge the orchestrator's prompt does not list the follow-up yet")

    happen(case, "merged", HELD)
    tick(case, capsys)

    assert state(case, capsys, row["id"]) == "due", f"The first tick after #{HELD} merges marks it due"
    told = due_notices(case, capsys, row["id"])
    assert len(told) == 1, f"The orchestrator is told once that the follow-up is due: {told}"
    assert told[0]["to"] == "orchestrator", told[0]
    section = orch_promise.prompt_section(case.store, "orchestrator")
    assert section.count(FOLLOW_UP) == 1, f"The orchestrator's next prompt lists the due follow-up once:\n{section}"
    assert row["id"] in section, section

    # Not kept: it stays raised (due, then late after its limit), never silently dropped.
    for minutes in (30, 60, 60):
        tick(case, capsys, minutes=minutes)
        assert state(case, capsys, row["id"]) in ("due", "late"), "An unkept follow-up stays raised"
        assert FOLLOW_UP in orch_promise.prompt_section(case.store, "orchestrator")
    assert len(due_notices(case, capsys, row["id"])) == 1, "Later ticks do not tell the orchestrator again"
    assert len(follow_up_promises(case, capsys, FOLLOW_UP)) == 1, "Ticks never double the promise"

    code, out, err = run(case, capsys, "orch", "promise", "done", row["id"])
    assert code == 0, f"fa orch promise done {row['id']} keeps the promise ({code}): {err}{out}"
    assert follow_up_promises(case, capsys, FOLLOW_UP) == [], "A kept follow-up is no longer open"
    assert FOLLOW_UP not in orch_promise.prompt_section(case.store, "orchestrator")
    tick(case, capsys)
    assert follow_up_promises(case, capsys, FOLLOW_UP) == [], "A kept follow-up does not come back"


def test_a_merge_of_another_issue_does_not_make_the_follow_up_due(tick_case, capsys):
    case = tick_case
    request_id = held_world(case, capsys)
    answer(case, capsys, request_id, f"ship with follow-up: {FOLLOW_UP}")
    row = the_promise(case, capsys, FOLLOW_UP)

    happen(case, "merged", OTHER)
    tick(case, capsys)
    tick(case, capsys, minutes=60)

    assert state(case, capsys, row["id"]) == "waiting", f"Only #{HELD} merging makes its follow-up due"
    assert due_notices(case, capsys, row["id"]) == []
