"""Locked review-direction acceptance: one confirmation task with first-round evidence.

Break-it: rejects another wide round, a confirmer without first-round evidence,
and confirmation limited to the cited line. The fake supplies judgments,
never the script.
"""

import re
from foragent.executors import ExecResult
from foragent.runner import run_issue
from test_review_perspective_fanout import direction_case, install_review
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


FIRST = (
    "VERDICT: FAIL\n"
    "src/runtime/ownership.py:1 — an old writer overwrites the current receipt\n"
    "  class: stale-receipt-owner\n"
    "src/ordinary.py:1 — rollback drops the pending receipt\n"
    "  class: partial-receipt-rollback"
)


def assert_confirmation(prompt):
    text = " ".join(prompt.lower().split())
    for evidence in ("stale-receipt-owner", "partial-receipt-rollback",
                     "old writer overwrites the current receipt", "rollback drops the pending receipt"):
        assert evidence in text, f"Confirmation lost first-round evidence: {evidence}"
    assert re.search(r"(?:confirm|verif\w*|recheck).{0,160}(?:fix|repair|resolv)", text), (
        "Round 2 must be instructed to confirm the first-round fixes"
    )
    assert re.search(r"(?:all|every|across|throughout).{0,100}(?:instance|changed code|change|defect class)", text), (
        "Confirmation must cover each defect class everywhere in the change"
    )
    assert re.search(r"(?:only|limit\w*).{0,140}(?:confirm|verif\w*|first.round|known.{0,30}class)", text), (
        "Later rounds must limit their task to confirmation"
    )


def confirming_case(git_history, tmp_path, monkeypatch, final):
    case = direction_case(git_history, tmp_path, monkeypatch, review_max=1, max_retries=1)
    original = case.implementer.steps[0]
    case.implementer.steps = [original, original]
    first, confirmations = [], []

    def review(**kwargs):
        # First-round reviewers and their merger get the same scripted defect
        # evidence. The next lane invocation is the observable round boundary.
        target = first if len(case.implementer.calls) == 1 else confirmations
        target.append(kwargs)
        return ExecResult(0, False, FIRST if target is first else final, kwargs["log_path"])

    install_review(case, review)
    outcome = run_issue(1, case.ctx)
    assert len(case.implementer.calls) == 2, "Return the collected first-round findings once for rework"
    assert len(confirmations) == 1, "Round 2 needs exactly one confirming reviewer and no merger"
    return case, outcome, confirmations[0]


def test_round_two_has_one_confirmer_with_all_first_round_classes(git_history, tmp_path, monkeypatch):
    case, outcome, call = confirming_case(git_history, tmp_path, monkeypatch, "VERDICT: PASS")
    assert_confirmation(call["prompt"])
    assert outcome == "merged" and len(case.tracker.merged_prs) == 1


