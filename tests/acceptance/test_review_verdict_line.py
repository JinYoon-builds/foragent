"""Locked acceptance for review-verdict-line; publish under tests/acceptance/.

The public runner keeps D44 finding handoff while separating missing protocol
from defect accounting. Scripted responses supply the fixture's judgments;
production remains responsible for agent context, transport and recording.
Break-it: reject false defects, dropped finding handoff, empty-text rework,
lost/duplicate round records, missing native context, and ignored real FAILs.
"""

import json

import pytest

from foragent.executors import ExecResult
from foragent.runner import run_issue
from test_orch_review_handoff import make_case
from test_review_perspective_fanout import direction_case, install_review
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


FINDING = "The retry path loses the user's selected locale.\nSee feature.txt:1."
NO_FINDINGS = "Review completed.\n\n(none)"
EVIDENCE = "verdict-line-acceptance-evidence"


def review_events(case):
    path = case.store.root / "orch-events.jsonl"
    events = [event for line in path.read_text().splitlines()
              if (event := json.loads(line)).get("kind") == "review" and event.get("issue") == 1]
    assert events, "Every completed review round needs a recorded verdict"
    rounds = [event["round"] for event in events]
    assert len(rounds) == len(set(rounds)), "Record each round once"
    return events


def response(kwargs, text):
    # Observe the public transport option, without prescribing the lane's
    # implementation. Native Claude wraps reviewer text in a result document.
    message = json.dumps({"result": text}) if kwargs.get("review_level") is not None else text
    return ExecResult(0, False, message, kwargs["log_path"])


def ordinary_case(git_history, tmp_path, monkeypatch, reviewer="codex", **settings):
    return make_case(
        git_history, tmp_path, monkeypatch, reviewer=reviewer,
        executor="claude" if reviewer == "codex" else "codex",
        **{"skill_libraries": [], "test_command": "true", "language": "en",
           "review_max": 0, "max_retries": 1, **settings},
    )


def assert_no_defect_hold(case):
    comments = "\n".join(comment.body for comment in case.tracker.list_comments(1))
    assert not any(f"<!-- foragent:blocked reason={reason} -->" in comments
                   for reason in ("risk-high", "review-dispute")), (
        "Missing verdicts must not consume the defect budget or create a defect hold: " + comments
    )
    assert "Reviewer still found defects" not in comments
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}


def assert_no_findings_round(case, outcome):
    # A missing verdict with no finding may be re-reviewed once or treated as
    # no findings. Both routes finish this all-green fixture without code rework.
    assert_no_defect_hold(case)
    assert outcome == "merged" and len(case.tracker.merged_prs) == 1
    assert len(case.implementer.calls) == 1, "An empty/no-finding review is not implementation rework"
    assert case.store.get_run(1).attempts == 1
    events = review_events(case)
    assert events[0]["verdict"] == "UNUSABLE", "Keep the missing-verdict round visible as unusable"
    assert all(event["verdict"] in {"UNUSABLE", "PASS"} and event["findings"] == 0
               for event in events), events
    return events


@pytest.mark.parametrize("reviewer,raw", [
    ("codex", "(none)"),
    ("codex", ""),
    ("claude", "(none)"),
    ("claude", NO_FINDINGS),
], ids=["plain-none", "empty-result", "native-none", "native-preamble"])
def test_missing_verdict_without_findings_is_unusable_but_not_rework(
    git_history, tmp_path, monkeypatch, reviewer, raw,
):
    case = ordinary_case(git_history, tmp_path, monkeypatch, reviewer)
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        return response(kwargs, raw if len(calls) == 1 else "VERDICT: PASS")

    case.reviewer.run = review
    outcome = run_issue(1, case.ctx)
    events = assert_no_findings_round(case, outcome)
    assert 1 <= len(calls) <= 2, "Re-review once or accept the round as no findings"
    assert len(events) <= len(calls), "No extra round receipts"
    if len(events) == 2:
        assert events[-1]["verdict"] == "PASS"


@pytest.mark.parametrize("reviewer,count,budget,raw", [
    ("codex", 1, 0, FINDING),
    ("codex", 3, 2, FINDING),
    ("claude", 1, 0, FINDING),
    ("claude", 1, 0, "feature.txt:1 — the retry path loses the selected locale"),
], ids=["zero-defect-budget", "three-round-risk-high-regression", "native-finding",
        "native-recognized-finding-without-verdict"])
def test_missing_verdict_with_findings_keeps_handoff_without_spending_defect_budget(
    git_history, tmp_path, monkeypatch, reviewer, count, budget, raw,
):
    case = ordinary_case(git_history, tmp_path, monkeypatch, reviewer,
                         review_max=budget, max_retries=count)
    case.implementer.steps = [case.implement] * (count + 1)
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        return response(kwargs, raw if len(calls) <= count else "VERDICT: PASS")

    case.reviewer.run = review
    outcome = run_issue(1, case.ctx)
    assert_no_defect_hold(case)
    assert outcome == "merged" and len(case.tracker.merged_prs) == 1
    assert len(calls) == count + 1 and len(case.implementer.calls) == count + 1
    assert case.store.get_run(1).attempts == count + 1
    for call in case.implementer.calls[1:]:
        assert "A reviewer said (verbatim):\n" + raw in call["prompt"]
        assert "For each reviewer point either fix it or write `.foragent/REBUTTAL.md`" in call["prompt"]
    events = review_events(case)
    assert len(events) == count + 1
    assert all(event["verdict"] == "UNUSABLE" and event["findings"] == 0
               for event in events[:-1]), events
    assert events[-1]["verdict"] == "PASS"


@pytest.mark.parametrize("missing_at", ["perspective", "merge"])
def test_panel_no_finding_output_does_not_become_implementation_rework(
    git_history, tmp_path, monkeypatch, missing_at,
):
    case = direction_case(git_history, tmp_path, monkeypatch)
    malformed = []

    def review(**kwargs):
        # The fixture marker is present only in merged input. It identifies
        # transport calls, not the semantic meaning of reviewer prose.
        merging = EVIDENCE in kwargs["prompt"]
        if not malformed and merging == (missing_at == "merge"):
            malformed.append(True)
            return response(kwargs, NO_FINDINGS)
        return response(kwargs, "VERDICT: PASS\nNOTE: " + EVIDENCE)

    install_review(case, review)
    outcome = run_issue(1, case.ctx)
    assert malformed, "Exercise the selected missing-verdict boundary through run_issue"
    assert_no_findings_round(case, outcome)


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
def test_native_and_codex_reviewers_receive_the_same_verdict_contract(
    git_history, tmp_path, monkeypatch, reviewer,
):
    case = ordinary_case(git_history, tmp_path, monkeypatch, reviewer, max_retries=0)
    contexts = []

    def review(**kwargs):
        local = kwargs["worktree"] / "CLAUDE.local.md"
        context = kwargs["prompt"] + (local.read_text() if local.is_file() else "")
        context += "\n".join(pr.body for pr in case.tracker.created_prs)
        contexts.append(context)
        return response(kwargs, "VERDICT: PASS")

    case.reviewer.run = review
    outcome = run_issue(1, case.ctx)
    assert contexts, "Capture the actual reviewing agent's context"
    for context in contexts:
        assert "VERDICT: PASS" in context and "VERDICT: FAIL" in context, (
            "Native Claude must receive the verdict contract that Codex receives"
        )
        assert "exactly one" in context.lower(), "The contract requires one verdict line"
    assert outcome == "merged" and len(case.tracker.merged_prs) == 1
    assert len(case.implementer.calls) == 1


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
def test_real_failure_still_blocks_once_with_recorded_findings(
    git_history, tmp_path, monkeypatch, reviewer,
):
    # Positive control: replacing every result with no findings is not a fix.
    case = ordinary_case(git_history, tmp_path, monkeypatch, reviewer)
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        return response(kwargs, "VERDICT: FAIL\nfeature.txt:1 — the approved feature loses data")

    case.reviewer.run = review
    assert run_issue(1, case.ctx) == "held"
    assert len(calls) == 1 and len(case.implementer.calls) == 1
    assert not case.tracker.merged_prs
    events = review_events(case)
    assert len(events) == 1 and events[0]["verdict"] == "FAIL" and events[0]["findings"] == 1
