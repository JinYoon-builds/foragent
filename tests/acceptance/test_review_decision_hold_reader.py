"""Architect r940: hold retries early and consume the PM's review decision."""

from datetime import datetime, timedelta

from foragent import orch_events, orch_inbox, runner
from foragent.reasons import Reason
from foragent.runner import run_issue
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_blocks_major_only import ASSESSMENT, FINDING
from test_review_grounded_fail import ordinary
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


HOLD_REASON = "Two failed rounds of the same class; awaiting PM decision."


def pending_review_decision(git_history, tmp_path, monkeypatch):
    """Reach the existing two-round hold through the real runner and fake agents."""
    second = "feature.txt:9 — Another saved result is wrong.\n  class: result persistence"
    case, script = ordinary(
        git_history, tmp_path, monkeypatch,
        [f"VERDICT: FAIL\n{FINDING}", f"VERDICT: FAIL\n{second}", "VERDICT: PASS"],
        ASSESSMENT, review_max=5, max_retries=5,
    )
    case.implementer.steps *= 3
    assert run_issue(1, case.ctx) == "held"
    assert len(case.implementer.calls) == len(script.reviewer_calls) == 2
    approval = case.store.load_approval(1)
    reviews = orch_events.events(case.store, "review")
    assert [(row["issue"], row["round"], row["verdict"], row["finding_classes"], row["body_sha"])
            for row in reviews] == [
        (1, 1, "FAIL", ["result persistence"], approval.body_sha),
        (1, 2, "FAIL", ["result persistence"], approval.body_sha),
    ]
    question, = orch_inbox.questions(case.store, include_closed=True)
    assert question["status"] == "open" and not question.get("answered_at")
    assert question["issues"] == [1]
    assert question["options"] == ["Ship with follow-ups", "Narrow the scope"]
    return case, script, question


def queue_retry(case):
    case.tracker.remove_labels(1, ["blocked"])
    case.tracker.add_labels(1, ["running"])


def test_repeated_class_hold_happens_before_implementation(git_history, tmp_path, monkeypatch):
    case, script, question = pending_review_decision(git_history, tmp_path, monkeypatch)
    implementation_calls = len(case.implementer.calls)
    reviewer_calls = len(script.reviewer_calls)
    checks = []
    check = runner._check

    def observe_check(*args, **kwargs):
        checks.append((args, kwargs))
        return check(*args, **kwargs)

    monkeypatch.setattr(runner, "_check", observe_check)
    queue_retry(case)

    assert run_issue(1, case.ctx) == "held"

    assert "blocked" in case.tracker.get_issue(1).labels
    assert case.store.load_flag(1).reason == Reason.REVIEW_DEFECTS
    assert case.store.get_run(1).hold_reason == Reason.REVIEW_DEFECTS.value
    assert any(HOLD_REASON in comment.body for comment in case.tracker.list_comments(1))
    assert orch_inbox.questions(case.store, include_closed=True) == [question]
    assert len(script.reviewer_calls) == reviewer_calls
    assert len(case.implementer.calls) == implementation_calls, (
        "An unanswered review decision must hold the retry before an implementer starts"
    )
    assert checks == [], "The pending decision must hold the retry before baseline/checks start"


def test_answered_review_decision_lets_the_next_run_reach_review(git_history, tmp_path, monkeypatch):
    case, script, question = pending_review_decision(git_history, tmp_path, monkeypatch)
    reviewer_calls = len(script.reviewer_calls)
    case.clock[0] += timedelta(minutes=1)
    # fa orch answer calls this API; no separate PM PASS/resolve operation is needed.
    answered = orch_inbox.answer(case.store, question["id"], "Ship with follow-ups")
    assert answered["status"] == "closed" and answered["answer"] == "Ship with follow-ups"
    assert all(datetime.fromisoformat(row["at"]) < datetime.fromisoformat(answered["answered_at"])
               for row in orch_events.events(case.store, "review"))
    assert orch_inbox.questions(case.store) == []
    case.clock[0] += timedelta(minutes=1)
    queue_retry(case)

    outcome = run_issue(1, case.ctx)

    assert len(script.reviewer_calls) == reviewer_calls + 1, (
        f"The PM's answered decision must release the old FAIL rounds; got {outcome!r}"
    )
    reviews = orch_events.events(case.store, "review")
    assert len(reviews) == 3 and reviews[-1]["verdict"] == "PASS"
    assert orch_inbox.questions(case.store, include_closed=True) == [answered]
