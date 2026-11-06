"""Acceptance test for conflict-recheck-overlap (review). Locked by Foragent; do not edit.

#1641 (2026-11-04/06): a reviewed PR waited to merge, main moved and conflicted,
and the run redid a full multi-perspective review and every related local test
although only the merge resolution was new.

Statement: a reviewed issue that conflicts with main, whose resolution changes only
the conflicting lines, keeps its PASS for its own change; only the resolution
(the difference between a plain merge of the reviewed head with main and the
resolved result) is reviewed. Every review session after the resolution gets only
the resolution plus the PASS record, not the whole change again. The number of
sessions is free.

Real runner, Git, risk map and reviewer input. Only GitHub,
models and the clock are fake. The conflict comes from main moving during the
first review, so the existing publication path meets a real conflict.
"""

import json

import yaml

from foragent import runner
from foragent.executors import ExecResult
from test_review_pass_scope import PASS, case_for, content, implement
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import entry, offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


OWN_ONLY = "own_only.txt"
OWN_ONLY_TEXT = "OWN-UNCHANGED-RECEIPT-1641\n"
MAIN_ONLY = "main_only.txt"
MAIN_ONLY_TEXT = "MAIN-ONLY-RECEIPT-1641\n"


def reviewer_input(call):
    """Everything a reviewer session can read: its prompt plus the native scope file."""
    context = call["prompt"]
    local = call["worktree"] / "CLAUDE.local.md"
    if local.is_file():
        context += local.read_text()
    return context


def scope_lines(context):
    """Lines that tell a reviewer which paths the review covers: diff headers and
    risk-map "changed here" path lists. A full review of the whole change lists every
    PR path there; a resolution-only review lists only what the resolution changed."""
    return [
        line for line in context.splitlines()
        if line.startswith(("diff --git ", "--- ", "+++ ")) or "changed here" in line
    ]


def test_conflict_after_pass_reviews_only_the_resolution_not_the_whole_change_again(
    git_history, tmp_path, monkeypatch,
):
    # A catch-all risk-map entry: the first review is the full per-perspective panel.
    git_history.update({".foragent-risks.yml": yaml.safe_dump([entry(["**"])])})
    case = case_for(git_history, tmp_path, monkeypatch)
    moved = []

    def first(call):
        (call["worktree"] / OWN_ONLY).write_text(OWN_ONLY_TEXT)
        return implement(content())(call)

    def resolve(call):
        text = (call["worktree"] / "feature.txt").read_text()
        assert "<<<<<<<" in text, "Exercise a real conflict with main"
        # Plain merge result: main's far `base = 1` already folded in; only the
        # conflicting first lines remain. The resolution touches only those.
        assert "base = 1" in text
        (call["worktree"] / "feature.txt").write_text(content("resolved", 1, 1))
        return "Resolved the conflict"

    case.implementer.steps = [first, resolve]
    first_round, later = [], []

    def review(**call):
        if len(case.implementer.calls) == 1:
            first_round.append(call["prompt"])
            if not moved:
                # Main moves while the reviewed contents exist: the PASS head conflicts.
                case.history.update({"feature.txt": content("upstream", 0, 1), MAIN_ONLY: MAIN_ONLY_TEXT})
                moved.append(case.history.tip)
            text = PASS
        else:
            later.append(call)
            text = "VERDICT: PASS"
        if call.get("review_level") is not None:
            text = json.dumps({"result": text})
        return ExecResult(0, False, text, call["log_path"])

    case.reviewer.run = review
    outcome = runner.run_issue(1, case.ctx)

    log = case.store.log_path(1).read_text()
    assert len(case.implementer.calls) == 2, "One lane turn resolves the conflict; " + log[-3000:]
    assert len(first_round) > 1, "Guard: the catch-all risk map makes the first review a perspective panel"
    assert outcome == "merged", log[-3000:]
    assert later, "The conflict resolution after the PASS must still be reviewed"
    # Any number of reviewer sessions may review the resolution (a panel merger
    # may see only member verdicts); no session's input may hold the whole change again.
    contexts = [reviewer_input(call) for call in later]
    assert any("+accepted = 'resolved'" in context for context in contexts), (
        "The reviewer must receive the actual resolution lines"
    )
    assert all(
        "PASS-RECEIPT-47" in context for context in contexts if "+accepted = 'resolved'" in context
    ), "The issue keeps its PASS: every session reviewing the resolution gets that record"
    for index, context in enumerate(contexts):
        where = f"reviewer session {index + 1} of {len(later)} after the resolution"
        assert "+accepted = 'reviewed'" not in context, (
            f"{where}: the issue's own reviewed change is not reviewed again"
        )
        assert OWN_ONLY_TEXT.strip() not in context, f"{where}: the issue's own unchanged file is not reviewed again"
        assert MAIN_ONLY_TEXT.strip() not in context and "+base = 1" not in context, (
            f"{where}: code that only arrived from main is not part of the resolution"
        )
        scoped = scope_lines(context)
        assert not [line for line in scoped if OWN_ONLY in line], (
            f"{where}: the issue's own unchanged file is not listed as part of the change under review "
            f"again (a full review of the whole change); observed {[l for l in scoped if OWN_ONLY in l]}"
        )
        assert not [line for line in scoped if MAIN_ONLY in line], (
            f"{where}: a file that only arrived from main is not part of the resolution under review"
        )
    assert case.tracker.merged_prs, "The PASS plus the resolution review lets the PR merge"
