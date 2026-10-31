"""Locked lane prompt: rejects cited-line-only repair and a missing PR searched list."""

import re
from types import SimpleNamespace
import pytest
from foragent.executors import ExecResult
from foragent.runner import run_issue
from test_orch_review_handoff import make_case
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


def is_lane_turn(kwargs):
    return re.search(r"(?:You are|Continue) implementing GitHub issue #1\b", kwargs["prompt"]) is not None


@pytest.mark.parametrize("resume", [False, True], ids=["fresh-lane", "resumed-lane"])
def test_rework_prompt_requires_class_wide_repairs_and_a_searched_list_in_the_pr(
    git_history, tmp_path, monkeypatch, resume,
):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
                     review_max=1, max_retries=1, language="en")
    lane_calls, review_calls = [], []

    def execute(**kwargs):
        if is_lane_turn(kwargs):
            lane_calls.append(kwargs)
            case.implement(kwargs)
            return ExecResult(0, False, "Implemented", kwargs["log_path"],
                              session_id="offline-lane-session" if resume else None)
        review_calls.append(kwargs)
        return ExecResult(0, False, FIRST if len(review_calls) == 1 else "VERDICT: PASS", kwargs["log_path"])

    case.ctx.executor_factory = lambda name: SimpleNamespace(run=execute)
    assert run_issue(1, case.ctx) == "merged"
    assert len(lane_calls) == 2
    assert (lane_calls[1].get("resume_session_id") is not None) is resume
    prompt = " ".join(lane_calls[1]["prompt"].lower().split())
    assert "stale-receipt-owner" in prompt and "partial-receipt-rollback" in prompt
    assert re.search(r"defect class.{0,160}(?:every|all|across).{0,120}(?:instance|change|code)", prompt)
    assert re.search(r"(?:fix|repair).{0,100}(?:each|all|every)|(?:each|all|every).{0,100}(?:fix|repair)", prompt)
    assert re.search(r"(?:search\w*|looked for|inspect\w*).{0,200}(?:\bpr\b|pull request)|"
                     r"(?:\bpr\b|pull request).{0,200}(?:search\w*|looked for|inspect\w*)", prompt), (
        "The lane must be asked to put what it searched in the PR"
    )
    assert re.search(r"(?:list|record|document).{0,120}(?:search\w*|looked for|inspect\w*)|"
                     r"(?:search\w*|looked for|inspect\w*).{0,120}(?:list|record|document)", prompt), (
        "The repair prompt must request an explicit searched list, not only the cited-line fix"
    )
