"""Locked acceptance for review-verdict-line; publish under tests/acceptance/.

Exercise run_issue with scripted transports, real local Git and the real journal.
Break-it: reject treating a missing verdict as FAIL or PASS, sending it back to
the implementer, losing/duplicating the round receipt, or ignoring a real FAIL.
No production parser, review-loop helper or prompt constant supplies the oracle.
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


MISSING = "Review completed.\n\n(none)"
EVIDENCE = "verdict-line-acceptance-evidence"


def review_events(case):
    path = case.store.root / "orch-events.jsonl"
    return [event for line in path.read_text().splitlines()
            if (event := json.loads(line)).get("kind") == "review" and event.get("issue") == 1]


def response(kwargs, text, *, native_pass=False):
    # Native Claude wraps its text in JSON. A replacement prompt transport may
    # use the ordinary text contract; the implementation chooses the transport.
    if kwargs.get("review_level") is not None:
        text = json.dumps({"result": "(none)" if native_pass else text})
    return ExecResult(0, False, text, kwargs["log_path"])


def assert_transient_or_recovered(case, outcome):
    events = review_events(case)
    assert events, "Every completed review round needs a recorded verdict"
    rounds = [event["round"] for event in events]
    assert len(rounds) == len(set(rounds)), "Record each round once"
    assert all(event["verdict"] in {"PASS", "UNUSABLE"} and event["findings"] == 0
               for event in events), events
    assert len(case.implementer.calls) == 1, "Missing review protocol must not request code rework"
    comments = "\n".join(comment.body for comment in case.tracker.list_comments(1))
    assert not any(f"<!-- foragent:blocked reason={reason} -->" in comments
                   for reason in ("risk-high", "review-dispute")), (
        "A missing verdict must not spend the defect budget or claim defects: " + comments
    )
    if case.tracker.merged_prs:
        assert outcome == "merged" and len(case.tracker.merged_prs) == 1
        assert len(events) <= 2 and events[-1]["verdict"] == "PASS", (
            "A malformed review alone cannot authorize merge; obtain a valid review first"
        )
        assert sum(event["verdict"] == "PASS" for event in events) == 1
    else:
        assert outcome != "merged" and events[-1]["verdict"] == "UNUSABLE", (
            "The lane may stop with a recorded transient review failure"
        )
        assert outcome not in {"crashed", "executor-error", "timeout"}, (
            "An unrelated crash or timeout is not a handled transient review failure"
        )


@pytest.mark.parametrize("reviewer,raw", [
    ("codex", "(none)"),
    ("claude", MISSING),
    ("claude", "**(none)**\n\nNo findings."),
], ids=["plain-missing-verdict", "native-preamble", "native-markdown"])
def test_missing_verdict_never_becomes_an_implementation_defect(
    git_history, tmp_path, monkeypatch, reviewer, raw,
):
    case = make_case(
        git_history, tmp_path, monkeypatch, reviewer=reviewer,
        executor="claude" if reviewer == "codex" else "codex",
        skill_libraries=[], test_command="true", language="en", review_max=0, max_retries=1,
    )
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        return response(kwargs, raw if len(calls) == 1 else "VERDICT: PASS",
                        native_pass=len(calls) > 1)

    case.reviewer.run = review
    outcome = run_issue(1, case.ctx)
    assert_transient_or_recovered(case, outcome)
    assert 1 <= len(calls) <= 2, "Re-ask at most once, or finish as a transient failure"
    assert len(review_events(case)) <= len(calls), "No duplicate round receipts"
    if outcome == "merged":
        assert len(calls) == 2, "A valid reply must replace the malformed reply before merge"


@pytest.mark.parametrize("missing_at", ["perspective", "merge"])
def test_missing_panel_verdict_is_not_a_defect_from_the_authoritative_review(
    git_history, tmp_path, monkeypatch, missing_at,
):
    case = direction_case(git_history, tmp_path, monkeypatch)
    calls, malformed, valid_merges = [], [], []

    def review(**kwargs):
        merging = EVIDENCE in kwargs["prompt"]
        calls.append(merging)
        if not malformed and merging == (missing_at == "merge"):
            malformed.append(True)
            return response(kwargs, MISSING)
        if merging:
            valid_merges.append(True)
        return response(kwargs, "VERDICT: PASS\nNOTE: " + EVIDENCE)

    install_review(case, review)
    outcome = run_issue(1, case.ctx)
    assert malformed, "The public run must exercise the selected missing-verdict boundary"
    assert_transient_or_recovered(case, outcome)
    if outcome == "merged":
        assert len(valid_merges) == 1, "A valid authoritative merge must authorize publication exactly once"
    elif missing_at == "perspective":
        assert not any(calls), "A merger must not conceal an unusable perspective"


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
def test_ordinary_review_has_an_explicit_effort_and_the_verdict_contract(
    git_history, tmp_path, monkeypatch, reviewer,
):
    case = make_case(
        git_history, tmp_path, monkeypatch, reviewer=reviewer,
        executor="claude" if reviewer == "codex" else "codex",
        skill_libraries=[], test_command="true", language="en",
    )
    calls = []

    def review(**kwargs):
        local = kwargs["worktree"] / "CLAUDE.local.md"
        context = kwargs["prompt"] + (local.read_text() if local.is_file() else "")
        calls.append((kwargs, context))
        return response(kwargs, "VERDICT: PASS", native_pass=True)

    case.reviewer.run = review
    assert run_issue(1, case.ctx) == "merged"
    assert len(calls) == 1
    kwargs, context = calls[0]
    effort = kwargs.get("review_level") or kwargs.get("effort")
    assert effort and effort not in {"low", "minimal"}, (
        f"Review must not inherit an unknown effort or select a mode that drops tests/contract: {effort!r}"
    )
    assert "VERDICT: PASS" in context and "VERDICT: FAIL" in context, (
        "The actual reviewing agent needs the verdict contract, including native review scope"
    )
    events = review_events(case)
    assert len(events) == 1 and events[0]["verdict"] == "PASS" and events[0]["findings"] == 0


def test_real_failure_still_blocks_once_with_recorded_findings(
    git_history, tmp_path, monkeypatch,
):
    # Positive control: replacing every result with PASS/UNUSABLE is not a fix.
    case = make_case(
        git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
        language="en", review_max=0, max_retries=1,
    )
    case.reviewer.steps = ["VERDICT: FAIL\nfeature.txt:1 — the approved feature loses data"]
    assert run_issue(1, case.ctx) == "held"
    assert len(case.reviewer.calls) == 1 and len(case.implementer.calls) == 1
    assert not case.tracker.merged_prs
    events = review_events(case)
    assert len(events) == 1 and events[0]["verdict"] == "FAIL" and events[0]["findings"] == 1
