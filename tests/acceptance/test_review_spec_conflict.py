"""Acceptance test for review-spec-conflict. Locked by Foragent; do not edit."""

from conftest import ACCEPTANCE
from test_orch_review_handoff import make_case

CONFLICT = (f"SPEC-CONFLICT: {ACCEPTANCE}:1 requires a failed terminal show to count as closed; "
            "the finding wants an unknown result to keep the talk open")
SPEC_QUESTION = "<!-- foragent:blocked reason=spec-question -->"


def test_a_spec_conflict_from_review_becomes_a_spec_question_without_a_retry(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch)
    case.implementer.steps = [case.implement, case.implement]
    case.reviewer.steps = [f"VERDICT: FAIL\n{CONFLICT}\n", "VERDICT: PASS"]
    outcome = run_issue(1, case.ctx)
    assert outcome == "spec-question", outcome
    assert len(case.implementer.calls) == 1, "A spec conflict must not send the work back to the implementer"
    assert len(case.reviewer.calls) == 1
    labels = case.tracker.get_issue(1).labels
    assert "blocked" in labels and "needs-human" not in labels, labels
    questions = [c.body for c in case.tracker.list_comments(1) if c.body.startswith(SPEC_QUESTION)]
    assert len(questions) == 1, [c.body[:80] for c in case.tracker.list_comments(1)]
    assert ACCEPTANCE in questions[0] and "unknown result to keep the talk open" in questions[0]
    assert case.tracker.merged_prs == []


def test_the_reviewer_is_told_how_to_report_a_conflict_with_a_locked_test(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch)
    case.reviewer.steps = ["VERDICT: PASS"]
    run_issue(1, case.ctx)
    prompt = " ".join(case.reviewer.calls[0]["prompt"].split())
    assert "SPEC-CONFLICT:" in prompt
    assert "locked acceptance test" in prompt.lower()


def test_an_ordinary_failed_review_still_returns_to_the_implementer(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue

    case = make_case(git_history, tmp_path, monkeypatch)
    case.implementer.steps = [case.implement, case.implement]
    case.reviewer.steps = ["VERDICT: FAIL\nfeature.txt:1 loses the selected locale", "VERDICT: PASS"]
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 2
    assert not any(c.body.startswith(SPEC_QUESTION) for c in case.tracker.list_comments(1))
