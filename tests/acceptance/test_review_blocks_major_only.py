"""Jin 2026-11-05: consequence-based blocking and bounded same-class rework."""

import pytest

from foragent import orch_events, orch_inbox
from foragent.runner import run_issue
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_grounded_fail import ordinary
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


FINDING = "feature.txt:1 — A saved result is wrong.\n  class: result persistence"
ASSESSMENT = {
    "score": 100, "critical_path": False, "exists": True,
    "wrong_result": True, "ordinary_input": True, "irreversible": False,
    "finding_class": "result persistence", "goal_line": None, "locked_test": None,
    "breaking_input": "Save the result of an ordinary request and read it back.",
    "spec_question": None,
}


def followups(case):
    return [issue for issue in case.tracker.list_issues() if issue.number != 1]


def test_wrong_result_on_ordinary_input_blocks_even_with_a_low_score(git_history, tmp_path, monkeypatch):
    case, script = ordinary(git_history, tmp_path, monkeypatch, [f"VERDICT: FAIL\n{FINDING}"],
                            {**ASSESSMENT, "score": 5})
    assert run_issue(1, case.ctx) == "held"
    assert not case.tracker.merged_prs and followups(case) == []
    assert len(script.scorings) == len(script.reviewer_calls) == len(case.implementer.calls) == 1
    scores = orch_events.events(case.store, "review-score")
    assert len(scores) == 1 and scores[0]["outcome"] == "blocks"


@pytest.mark.parametrize("harm", ["loses unsaved work", "deletes data", "sends a message outside"])
def test_irreversible_harm_blocks_even_on_rare_input(git_history, tmp_path, monkeypatch, harm):
    finding = f"feature.txt:1 — A rare interruption {harm}."
    assessment = {**ASSESSMENT, "score": 5, "ordinary_input": False, "irreversible": True,
                  "wrong_result": False,
                  "breaking_input": f"A rare interruption {harm}."}
    case, script = ordinary(git_history, tmp_path, monkeypatch, [f"VERDICT: FAIL\n{finding}"], assessment)
    assert run_issue(1, case.ctx) == "held"
    assert not case.tracker.merged_prs and followups(case) == []
    assert len(script.scorings) == len(script.reviewer_calls) == len(case.implementer.calls) == 1


def test_rare_reversible_bug_passes_and_creates_one_followup_per_finding(git_history, tmp_path, monkeypatch):
    findings = [FINDING, "feature.txt:2 — A rare alias can be retried.\n  class: command alias"]
    assessment = {**ASSESSMENT, "ordinary_input": False,
                  "breaking_input": "A rare alias fails; retrying with its standard spelling succeeds."}
    case, script = ordinary(git_history, tmp_path, monkeypatch,
                            ["VERDICT: FAIL\n" + "\n".join(findings)], assessment)
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.tracker.merged_prs) == 1
    assert len(script.reviewer_calls) == len(case.implementer.calls) == 1
    assert len(script.scorings) == 2
    issues = followups(case)
    assert len(issues) == 2
    for finding in findings:
        matching = [issue for issue in issues if finding in issue.body]
        assert len(matching) == 1 and "Source issue: #1" in matching[0].body
    # Replaying scoring must reuse the two tracker issues, even without its local receipts.
    from foragent import review_scoring
    from foragent.issue import parse_body
    from foragent.run_review import ReviewResult

    review_scoring.score(1, case.ctx, ReviewResult("FAIL", "\n".join(findings)), 1,
                         git=case.git, parsed=parse_body(case.tracker.get_issue(1).body))
    assert len(followups(case)) == 2


@pytest.mark.parametrize("note", ["slower", "less tidy", "wording"])
def test_no_wrong_result_passes_despite_a_goal_quote(git_history, tmp_path, monkeypatch, note):
    finding = f"feature.txt:1 — {note}, but the result is correct."
    assessment = {**ASSESSMENT, "wrong_result": False, "goal_line": "Implement the feature."}
    case, script = ordinary(git_history, tmp_path, monkeypatch, [f"VERDICT: FAIL\n{finding}"], assessment)
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.tracker.merged_prs) == 1
    assert len(script.scorings) == len(script.reviewer_calls) == len(case.implementer.calls) == 1
    issues = followups(case)
    assert len(issues) == 1 and finding in issues[0].body and "Source issue: #1" in issues[0].body


def test_two_failed_rounds_of_same_class_stop_before_third_and_ask_pm(git_history, tmp_path, monkeypatch):
    second = "feature.txt:9 — Another saved result is wrong.\n  class: result persistence"
    case, script = ordinary(git_history, tmp_path, monkeypatch,
                            [f"VERDICT: FAIL\n{FINDING}", f"VERDICT: FAIL\n{second}", "VERDICT: PASS"],
                            ASSESSMENT, review_max=5, max_retries=5)
    case.implementer.steps *= 3
    assert run_issue(1, case.ctx) == "held"
    assert not case.tracker.merged_prs
    assert len(script.reviewer_calls) == len(script.scorings) == len(case.implementer.calls) == 2
    assert len(case.tracker.created_prs) == 1
    assert FINDING in case.tracker.created_prs[0].body and second in case.tracker.created_prs[0].body
    reviews = orch_events.events(case.store, "review")
    assert [(event["round"], event["verdict"], event["finding_classes"]) for event in reviews] == [
        (1, "FAIL", ["result persistence"]), (2, "FAIL", ["result persistence"])]
    questions = orch_inbox.questions(case.store)
    assert len(questions) == 1
    question = questions[0]
    assert question["issues"] == [1] and "result persistence" in question["question"]
    assert question["options"] == ["Ship with follow-ups", "Narrow the scope"]
    assert question["recommend"] == 2
    assert "PM" in question["question"] and FINDING in question["question"] and second in question["question"]
    assert script.scorings[1][0]["previous_classes"] == ["result persistence"]
    # A new runner invocation cannot evade the two-round limit or duplicate the question.
    case.tracker.remove_labels(1, ["blocked"])
    case.tracker.add_labels(1, ["running"])
    assert run_issue(1, case.ctx) == "held"
    assert len(script.reviewer_calls) == len(script.scorings) == 2
    assert len(orch_inbox.questions(case.store)) == 1


def test_review_only_uses_the_same_two_round_limit_and_pm_inbox(git_history, tmp_path, monkeypatch):
    from foragent.runner import review_only

    case, script = ordinary(git_history, tmp_path, monkeypatch,
                            [f"VERDICT: FAIL\n{FINDING}"] * 2 + ["VERDICT: PASS"], ASSESSMENT)
    assert run_issue(1, case.ctx) == "held"
    assert len(script.reviewer_calls) == 1 and orch_inbox.questions(case.store) == []
    assert review_only(1, case.ctx) == "held"
    assert len(script.reviewer_calls) == len(script.scorings) == 2
    questions = orch_inbox.questions(case.store)
    assert len(questions) == 1 and questions[0]["issues"] == [1]
    assert review_only(1, case.ctx) == "held"
    assert len(script.reviewer_calls) == len(script.scorings) == 2
    assert len(orch_inbox.questions(case.store)) == 1
    assert not case.tracker.merged_prs


@pytest.mark.parametrize("repair_fails_once", [False, True], ids=["repair-passes", "repair-needs-rework"])
def test_pm_pass_resets_failed_rounds_when_ci_repair_is_queued(
        git_history, tmp_path, monkeypatch, repair_fails_once):
    from foragent import orch

    fail = f"VERDICT: FAIL\n{FINDING}"
    later = [fail, "VERDICT: PASS"] if repair_fails_once else ["VERDICT: PASS"]
    case, script = ordinary(git_history, tmp_path, monkeypatch, [fail, fail, *later],
                            ASSESSMENT, review_max=5, max_retries=5)

    def repair(call):
        case.implement(call)
        (call["worktree"] / "ci-repair.txt").write_text("repaired\n")

    case.implementer.steps = [case.implement, case.implement, repair, repair]
    assert run_issue(1, case.ctx) == "held"
    assert len(script.reviewer_calls) == 2
    question, = orch_inbox.questions(case.store)
    orch_inbox.answer(case.store, question["id"], "Ship with follow-ups")
    pr = case.tracker.created_prs[0].number
    approved_head = case.tracker.pr_head_sha(pr)
    case.tracker.set_pr_checks(pr, "fail")
    case.tracker.set_pr_failed_logs(pr, "CI requires a repair")

    summary = orch.resolve(case.ctx, 1, "pass", "Ship with follow-ups")

    assert "ci-failed; queued implementation retry" in summary
    assert len(script.reviewer_calls) == 2 and not case.tracker.merged_prs
    assert run_issue(1, case.ctx) == "merged"
    assert len(script.reviewer_calls) == len(case.implementer.calls) == 2 + len(later)
    assert len(script.scorings) == 2 + int(repair_fails_once)
    if repair_fails_once:
        assert script.scorings[2][0]["previous_classes"] == []
    assert orch_inbox.questions(case.store) == []
    assert len(orch_inbox.questions(case.store, include_closed=True)) == 1
    reviews = orch_events.events(case.store, "review")
    passed = [event for event in reviews if event["source"] == "orchestrator"]
    assert len(passed) == 1 and passed[0]["verdict"] == "PASS"
    assert passed[0]["body_sha"] == case.store.load_approval(1).body_sha
    merged = case.tracker.merged_prs[0]
    assert case.tracker.pr_head_sha(merged) != approved_head
