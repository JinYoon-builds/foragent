"""Locked review-direction acceptance: structure notes reach the architect.

Break-it: rejects blocking on structure alone, silently dropping a structure
proposal, journaling it only for the orchestrator, and hiding a real defect
beside a proposal. Inspect the next architect's actual input after reopening
state; a fake architect must not manufacture its own missing review context.
"""

from types import SimpleNamespace

import pytest

from foragent import executors, orch_architect
from foragent.executors import ExecResult
from foragent.runner import run_issue
from foragent.state import StateStore
from test_orch_review_handoff import make_case
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


PROPOSAL = "STRUCTURE-PROPOSAL-47: separate receipt parsing from delivery; this is outside the approved Goal."
DEFECT = "feature.txt:1 — an empty receipt crashes the approved confirmation flow"


@pytest.mark.parametrize("with_defect", [False, True], ids=["structure-only-passes", "real-defect-still-blocks"])
def test_structure_proposal_survives_merge_or_hold_and_reaches_the_next_architect(
    git_history, tmp_path, monkeypatch, with_defect,
):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
                     review_max=0, max_retries=1, language="en")
    case.reviewer.steps = [("VERDICT: FAIL\n" + DEFECT if with_defect else "VERDICT: PASS")
                           + "\nNOTE: " + PROPOSAL]
    outcome = run_issue(1, case.ctx)
    assert outcome == ("held" if with_defect else "merged")
    assert len(case.implementer.calls) == 1, "Structure proposals do not create a rework round"
    assert bool(case.tracker.merged_prs) is (not with_defect)
    blocked = [comment.body for comment in case.tracker.list_comments(1)
               if comment.body.startswith("<!-- foragent:blocked")]
    if with_defect:
        assert blocked and DEFECT in blocked[-1]
        assert PROPOSAL not in blocked[-1], "A structural proposal is not a merge blocker"

    # Reopen the durable state and start the existing public architect session.
    # This observes delivery, rather than pinning an inbox filename or schema.
    store = StateStore(case.store.root, now=case.ctx.now)
    (store.root / "logs").mkdir(exist_ok=True)
    received = []

    def architect(**kwargs):
        received.append(kwargs["prompt"])
        return ExecResult(0, False, "Proposal received.", kwargs["log_path"])

    monkeypatch.setattr(executors, "executor_for", lambda name: SimpleNamespace(run=architect))
    assert orch_architect.run_session(case.root, store, case.config, "Inspect recorded review proposals.",
                                      case.clock[0].isoformat(), case.ctx.now) == 0
    assert len(received) == 1
    # Either inline evidence or an explicit readable artifact pointer is valid.
    # A directory-wide pointer to state alone does not tell the agent what it lacked.
    delivered = PROPOSAL in received[0]
    for path in store.root.rglob("*"):
        if path.is_file() and PROPOSAL in path.read_text(encoding="utf-8", errors="replace"):
            delivered |= str(path) in received[0] or str(path.relative_to(store.root)) in received[0]
    assert delivered, "Recorded structure proposal never reached the architect's input"
