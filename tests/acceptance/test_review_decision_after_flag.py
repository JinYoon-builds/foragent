"""Acceptance test for review-decision-after-flag (#2868, 11-09).

The orchestrator answered the hold's review-decision request with 'ship with follow-up' at
20:38:50 and the gate wrote that hold's flag again at 20:38:55. A releasing answer to the
hold's request releases that hold whatever the order of the two writes, so
`fa orch retry N` queues the retry.

The hold and its request are reached through the real runner with fake agents (the same
world as test_review_decision_hold_reader and test_retry_same_class_refuse). The late flag
is the gate's own flag call with the gate's hold text; no new review-decision request is
raised (holds of a later, new request are out of scope).
"""

import sys
from datetime import datetime, timedelta

from foragent import actions, orch_events, orch_inbox, review_scoring, runner  # noqa: F401
from foragent.reasons import Reason
from foragent.runner import run_issue
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_orch_review_handoff import bind_cli
from test_review_blocks_major_only import ASSESSMENT, FINDING, review_decisions
from test_review_decision_hold_reader import pending_review_decision
from test_review_grounded_fail import ordinary
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


ANSWER = "ship with follow-up: persist the saved result before the reply in a follow-up issue"


def answer(case, request):
    """The orchestrator's `fa orch request-status ID done --answer ...` (the API it calls)."""
    answered = orch_inbox.set_request_status(case.store, request["id"], "done",
                                             answer=ANSWER, require_answer=True)
    assert answered["status"] == "done" and answered["answer"] == ANSWER
    return answered


def gate_flags_again(case):
    """The gate writes the hold's final flag with its own hold text (actions.flag, as run_review does)."""
    actions.flag(case.ctx, 1, Reason.REVIEW_DEFECTS, review_scoring.HOLD_REASON)


def bound_cli(case, monkeypatch):
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr("foragent.cli.RunContext", lambda **kwargs: case.ctx)
    return main


def assert_retry_queued(case, main, capsys, kicks):
    capsys.readouterr()
    retries = len(orch_events.events(case.store, "retry"))
    kicked = len(kicks)

    code = main(["orch", "retry", "1"])
    out = capsys.readouterr()
    text = out.out + out.err

    assert code in (0, None), (
        "A 'ship with follow-up' answer to the hold's request must release that hold even when "
        f"the gate wrote the hold's flag a few seconds after the answer; retry said: {text!r}")
    assert "queued implementation retry" in text, text
    assert "ready" in case.tracker.get_issue(1).labels, "The released retry must queue the issue"
    events = orch_events.events(case.store, "retry")
    assert len(events) == retries + 1, f"Exactly one retry must be recorded, got {events[retries:]!r}"
    assert len(kicks) == kicked + 1, (
        f"The queued retry kicks the gate exactly once, as an ordinary retry does: {kicks[kicked:]!r}")


def test_answer_seconds_before_the_final_flag_lets_retry_queue(
        git_history, tmp_path, monkeypatch, capsys, kicks):
    case, _script, request = pending_review_decision(git_history, tmp_path, monkeypatch)
    main = bound_cli(case, monkeypatch)
    case.clock[0] += timedelta(minutes=1)
    answered = answer(case, request)                        # 20:38:50
    case.clock[0] += timedelta(seconds=5)
    gate_flags_again(case)                                   # 20:38:55

    flag = case.store.load_flag(1)
    assert flag.reason == Reason.REVIEW_DEFECTS
    assert review_scoring.decided_at(1, case.ctx) < datetime.fromisoformat(
        str(flag.flagged_at).replace("Z", "+00:00")), "The flag must land after the answer here"
    assert review_decisions(case) == [answered], "No new review-decision request is raised"
    case.clock[0] += timedelta(minutes=1)

    assert_retry_queued(case, main, capsys, kicks)


def test_answer_landing_between_the_request_and_the_runners_flag_lets_retry_queue(
        git_history, tmp_path, monkeypatch, capsys, kicks):
    """The real runner raises the request and then flags; the answer lands in between (#2868)."""
    second = "feature.txt:9 — Another saved result is wrong.\n  class: result persistence"
    case, script = ordinary(
        git_history, tmp_path, monkeypatch,
        [f"VERDICT: FAIL\n{FINDING}", f"VERDICT: FAIL\n{second}", "VERDICT: PASS"],
        ASSESSMENT, review_max=5, max_retries=5,
    )
    case.implementer.steps *= 3
    answered = []
    flag = actions.flag

    def answer_then_flag(ctx, n, reason, *args, **kwargs):
        if reason == Reason.REVIEW_DEFECTS and not answered:
            request, = review_decisions(case, pending=True)
            answered.append(answer(case, request))          # the orchestrator answers first
            case.clock[0] += timedelta(seconds=5)           # the gate's flag lands 5 s later
        return flag(ctx, n, reason, *args, **kwargs)

    # Every module that flags through the shared actions.flag (the hold is flagged at publish).
    patched = [module for module in list(sys.modules.values())
               if getattr(module, "__name__", "").startswith("foragent")
               and getattr(module, "flag", None) is flag]
    for module in patched:
        monkeypatch.setattr(module, "flag", answer_then_flag)

    assert run_issue(1, case.ctx) == "held"

    assert answered, "The runner must reach the same-class hold with one review-decision request"
    assert case.store.load_flag(1).reason == Reason.REVIEW_DEFECTS
    assert review_decisions(case) == answered, "No new review-decision request is raised"
    for module in patched:
        monkeypatch.setattr(module, "flag", flag)
    main = bound_cli(case, monkeypatch)
    case.clock[0] += timedelta(minutes=1)

    assert_retry_queued(case, main, capsys, kicks)
