"""Locked class merge: rejects early/duplicate returns and text-only deduplication.

Raw judgments come from fake reviewers and the fake merging reviewer. The real
runner must collect all raw evidence, ask for grouping by defect class, and
return that authoritative result once, rather than unioning the raw verdicts.
"""

from dataclasses import replace
import re
from threading import Lock

import pytest

from foragent.executors import ExecResult
from foragent.runner import run_issue
from test_review_perspective_fanout import direction_case, install_review
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.mark.parametrize("kept", [True, False], ids=["real-classes-return-once", "dismissed-claims-pass"])
def test_all_raw_evidence_reaches_one_defect_class_merge_and_one_return(
    git_history, tmp_path, monkeypatch, kept,
):
    case = direction_case(git_history, tmp_path, monkeypatch)
    case.config = case.ctx.config = replace(case.config, review_reviewers=3)
    marker = "CLASS-EVIDENCE-47"
    canonical = (
        "src/runtime/ownership.py:1 — stale writers lose receipts across both entry paths\n"
        "  class: stale-owner\n"
        "src/ordinary.py:1 — rollback drops the pending receipt\n"
        "  class: partial-rollback"
    )
    messages, merge_calls = [], []
    lock = Lock()
    completed = [0]
    early = [False]

    def review(**kwargs):
        # This exact fixture marker identifies supplied evidence, not its meaning.
        if marker in kwargs["prompt"]:
            with lock:
                early[0] |= completed[0] != len(messages)
                merge_calls.append(kwargs)
            return ExecResult(0, False, "VERDICT: FAIL\n" + canonical if kept else "VERDICT: PASS",
                              kwargs["log_path"])
        with lock:
            index = len(messages)
            message = (
                f"VERDICT: FAIL\nsrc/runtime/ownership.py:{index + 1} — {marker}-{index}: "
                + ("an outdated owner overwrites a current receipt" if index % 2 == 0
                   else "a delayed writer replaces the newer receipt")
                + "\n  class: stale-owner\n"
                + f"src/ordinary.py:{index + 1} — interruption discards the pending receipt\n"
                + "  class: partial-rollback"
            )
            messages.append(message)
            completed[0] += 1
        return ExecResult(0, False, message, kwargs["log_path"])

    install_review(case, review)
    outcome = run_issue(1, case.ctx)
    assert len(merge_calls) == 1 and not early[0], "Collect every perspective before one final merge"
    prompt = merge_calls[0]["prompt"]
    assert all(message in prompt for message in messages), "The merger needs every raw claim, including duplicate classes"
    assert re.search(r"(?:group|merge|collect).{0,120}defect class|defect class.{0,120}(?:group|merge|collect)",
                     " ".join(prompt.lower().split())), "Merge findings by defect class, not only identical text"
    returns = [comment.body for comment in case.tracker.list_comments(1)
               if comment.body.startswith("<!-- foragent:blocked")]
    if kept:
        assert outcome == "held" and not case.tracker.merged_prs
        assert len(returns) == 1 and returns[0].count(canonical) == 1
        assert marker not in returns[0], "Return the class merge, not raw reviewer verdicts"
    else:
        assert outcome == "merged" and len(case.tracker.merged_prs) == 1
        assert returns == [], "Dismissed raw claims do not block a merge"
