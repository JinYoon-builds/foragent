"""Locked new-ground instructions: rejects fresh design holds and unconditional approval of new bugs.

The reviewing agent supplies the judgment; this test observes its actual instructions
and exercises both public verdict outcomes without interpreting prose in production.
"""

import re
import pytest
from test_review_confirm_only import confirming_case
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.mark.parametrize("real_defect", [False, True], ids=["new-structure-note", "new-real-bug"])
def test_confirmation_new_ground_only_returns_work_for_a_real_defect(
    git_history, tmp_path, monkeypatch, real_defect,
):
    reply = (
        "VERDICT: FAIL\nsrc/ordinary.py:2 — a newly exposed empty receipt crashes recovery\n"
        "  class: empty-receipt-crash"
        if real_defect else
        "VERDICT: PASS\nNOTE: New ground: move receipt parsing into a separate component outside the Goal."
    )
    case, outcome, call = confirming_case(git_history, tmp_path, monkeypatch, reply)
    text = " ".join(call["prompt"].lower().split())
    assert re.search(r"new.{0,160}(?:real|actual|concrete|reproducible).{0,60}(?:defect|bug)", text), (
        "New ground returns work only for a real defect"
    )
    assert outcome == ("held" if real_defect else "merged")
    assert bool(case.tracker.merged_prs) is (not real_defect)


