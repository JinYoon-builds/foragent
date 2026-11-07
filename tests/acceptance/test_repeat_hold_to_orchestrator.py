"""repeat-hold-to-orchestrator: a repeated-class review hold is the orchestrator's open work.

Jin 11-07 (pm-1107-orch-owns-review) supersedes the 11-05 sentence 'Two failed rounds of
the same class go to the PM'. Reached through the real runner with scripted agents.

Observable contract assumed here:
- The hold adds one orchestrator inbox request (orch_inbox.requests / `fa orch inbox`).
  It names the issue (an ``issue``/``issues`` field or ``#1`` in its text) and every
  defect class from both failed rounds, somewhere in the record.
- No PM question is created (orch_inbox.questions, closed ones included).
- The orchestrator records its answer the way `fa orch request-status <id> done --answer`
  does (orch_inbox.set_request_status(..., "done", answer=..., require_answer=True)).

Break-it: raising a PM question again, dropping or doubling the request (also on a retry),
leaving out the issue or one round's class, or ignoring the orchestrator's answer when the
next run decides whether to hold all fail here.
"""

import json
import re
from datetime import timedelta

import pytest

from foragent import orch_events, orch_inbox
from foragent.reasons import Reason
from foragent.runner import run_issue
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_blocks_major_only import ASSESSMENT
from test_review_grounded_fail import ordinary
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


FIRST = ("feature.txt:1 — A saved result is wrong.\n"
         "feature.txt:3 — An empty request is accepted.")
SECOND = ("feature.txt:9 — Another saved result is wrong.\n"
          "feature.txt:12 — The failure text names the wrong file.")
# Each round shares "result persistence" and has one class of its own.
CLASSES = {
    "A saved result is wrong.": "result persistence",
    "An empty request is accepted.": "input validation",
    "Another saved result is wrong.": "result persistence",
    "The failure text names the wrong file.": "error wording",
}
ALL_CLASSES = ("result persistence", "input validation", "error wording")


def assessment(finding):
    [name] = [name for line, name in CLASSES.items() if line in finding]
    return {**ASSESSMENT, "finding_class": name}


def names_issue(row, n=1):
    return (row.get("issue") == n or n in (row.get("issues") or [])
            or re.search(rf"#{n}\b", row.get("text", "")) is not None)


def hold_requests(case):
    return [row for row in orch_inbox.requests(case.store) if names_issue(row)]


def queue_retry(case):
    case.tracker.remove_labels(1, ["blocked"])
    case.tracker.add_labels(1, ["running"])


def repeated_hold(git_history, tmp_path, monkeypatch):
    case, script = ordinary(
        git_history, tmp_path, monkeypatch,
        [f"VERDICT: FAIL\n{FIRST}", f"VERDICT: FAIL\n{SECOND}", "VERDICT: PASS"],
        assessment, review_max=5, max_retries=5,
    )
    case.implementer.steps *= 3
    assert run_issue(1, case.ctx) == "held"
    assert len(case.implementer.calls) == len(script.reviewer_calls) == 2
    reviews = orch_events.events(case.store, "review")
    assert [(row["round"], row["verdict"], sorted(row["finding_classes"])) for row in reviews] == [
        (1, "FAIL", ["input validation", "result persistence"]),
        (2, "FAIL", ["error wording", "result persistence"]),
    ], "Fixture: two failed rounds sharing one class"
    return case, script


def the_request(case):
    rows = hold_requests(case)
    assert len(rows) == 1, (
        f"The repeated-class hold must add exactly one orchestrator request naming #1; got {rows!r}"
    )
    return rows[0]


def test_repeated_class_hold_goes_to_the_orchestrator_not_the_pm(git_history, tmp_path, monkeypatch):
    case, script = repeated_hold(git_history, tmp_path, monkeypatch)

    assert orch_inbox.questions(case.store, include_closed=True) == [], (
        "A machine-decidable repeated-class hold must not raise a PM question"
    )
    request = the_request(case)
    assert request["status"] != "done"
    record = json.dumps(request, ensure_ascii=False)
    for name in ALL_CLASSES:
        assert name in record, f"The request must name round classes from both rounds; missing {name!r}"
    assert "blocked" in case.tracker.get_issue(1).labels
    assert case.store.load_flag(1).reason == Reason.REVIEW_DEFECTS

    # A retry while the request is open still holds before work, without a second request or a question.
    implementation_calls, reviewer_calls = len(case.implementer.calls), len(script.reviewer_calls)
    case.clock[0] += timedelta(minutes=1)
    queue_retry(case)
    assert run_issue(1, case.ctx) == "held"
    assert len(case.implementer.calls) == implementation_calls
    assert len(script.reviewer_calls) == reviewer_calls
    assert [row["id"] for row in hold_requests(case)] == [request["id"]], (
        "A retry must not add a second request for the same hold"
    )
    assert orch_inbox.questions(case.store, include_closed=True) == []


@pytest.mark.parametrize("answer", ["ship with follow-up", "narrow"])
def test_orchestrator_answer_releases_the_hold(git_history, tmp_path, monkeypatch, answer):
    case, script = repeated_hold(git_history, tmp_path, monkeypatch)
    request = the_request(case)
    reviewer_calls = len(script.reviewer_calls)
    case.clock[0] += timedelta(minutes=1)
    closed = orch_inbox.set_request_status(case.store, request["id"], "done", answer=answer,
                                           require_answer=True)
    assert closed["status"] == "done" and closed["answer"] == answer
    case.clock[0] += timedelta(minutes=1)
    queue_retry(case)

    outcome = run_issue(1, case.ctx)

    assert len(script.reviewer_calls) == reviewer_calls + 1, (
        f"The orchestrator's {answer!r} answer must release the hold so the next run reaches review; "
        f"got {outcome!r}"
    )
    reviews = orch_events.events(case.store, "review")
    assert len(reviews) == 3 and reviews[-1]["verdict"] == "PASS"
    assert outcome == "merged"
    assert orch_inbox.questions(case.store, include_closed=True) == []
    assert [row["id"] for row in hold_requests(case)] == [request["id"]], (
        "Releasing the hold must not open another request"
    )
