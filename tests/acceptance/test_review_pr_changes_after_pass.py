"""Jin's 2026-11-02 decision: later reviews receive this PR's work, not main's.

Exercise public run/review outcomes and the actual input to the external reviewer.
Real offline Git histories prove provenance. Reviewer fakes supply all judgments.
"""

from dataclasses import replace

import pytest

from conftest import git_command
from foragent.runner import review_only, run_issue
from foragent.state import StateStore
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_pass_scope import PASS, capture_review, case_for, content, implement
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


MAIN_FILE = "main_receipts.txt"
MAIN_TEXT = "MAIN-ONLY-RECEIPT-20261102\n"
FINDING = "feature.txt:2 — the new retry loses a receipt"


def import_head(case, parent, text, *, merge=None):
    """Publish only into the test's bare remote, without commit/push/checkout."""
    stream = ("commit refs/heads/foragent/issue-1\n"
              "committer Offline Tests <tests@example.com> 1792486800 +0000\n"
              "data 7\nUpdate\n"
              f"from {parent}\n"
              + (f"merge {merge}\n" if merge else "")
              + f"M 100644 inline feature.txt\ndata {len(text.encode())}\n{text}\n"
              + f"M 100644 inline {MAIN_FILE}\ndata {len(MAIN_TEXT.encode())}\n{MAIN_TEXT}\n"
              + "\ndone\n")
    git_command(case.history.origin, "fast-import", "--quiet", "--force", input=stream)
    head = git_command(case.history.origin, "rev-parse", "refs/heads/foragent/issue-1")
    case.tracker.set_pr_head_sha(2, head)
    return head


def integrate(case, history, passed):
    case.history.update({MAIN_FILE: MAIN_TEXT})
    base = case.history.tip
    integrated = import_head(case, passed if history == "merge" else base, content(),
                             merge=base if history == "merge" else None)
    assert integrated != passed
    assert git_command(case.history.origin, "show", f"{integrated}:{MAIN_FILE}") == MAIN_TEXT.strip()
    assert git_command(case.history.origin, "diff", "--name-only", f"{base}...{integrated}") == "feature.txt"
    return integrated


def restart(case):
    case.ctx = replace(case.ctx, store=StateStore(case.store.root, now=case.ctx.now),
                       config=replace(case.config, review="fast"))


def assert_main_absent(context):
    assert MAIN_FILE not in context, "A file changed only by main must not enter review input"
    assert MAIN_TEXT.strip() not in context, "Main-only source must not enter review input"


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
@pytest.mark.parametrize("history", ["merge", "rebase"])
def test_main_only_files_are_absent_when_a_passed_head_is_reviewed_again(
    git_history, tmp_path, monkeypatch, history, reviewer,
):
    case = case_for(git_history, tmp_path, monkeypatch, reviewer=reviewer, review="careful")
    case.implementer.steps = [implement(content())]
    capture_review(case, [PASS])
    assert run_issue(1, case.ctx) == "pr-open"
    passed = case.tracker.pr_head_sha(2)
    integrate(case, history, passed)
    restart(case)
    contexts = capture_review(case, [PASS])
    assert review_only(1, case.ctx) == "merged"
    assert len(contexts) == 1 and len(case.implementer.calls) == 1
    assert passed in contexts[0] and "PASS-RECEIPT-47" in contexts[0]
    assert_main_absent(contexts[0])


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
def test_conflict_resolution_lines_reach_the_reviewer_and_can_block(
    git_history, tmp_path, monkeypatch, reviewer,
):
    case = case_for(git_history, tmp_path, monkeypatch, reviewer=reviewer)

    def first_review(call):
        case.history.update({"feature.txt": content("upstream", 0, 1), MAIN_FILE: MAIN_TEXT})
        return PASS

    def resolve(call):
        assert "<<<<<<<" in (call["worktree"] / "feature.txt").read_text()
        return implement(content("resolved", retry=2, base=1))(call)

    case.implementer.steps = [implement(content()), resolve]
    contexts = capture_review(case, [first_review, "VERDICT: FAIL\n" + FINDING])
    assert run_issue(1, case.ctx) == "held"
    assert len(contexts) == 2 and len(case.implementer.calls) == 2
    later = contexts[1]
    assert "+accepted = 'resolved'" in later and "+retry = 2" in later
    assert "PASS-RECEIPT-47" in later
    assert_main_absent(later)
    assert not case.tracker.merged_prs
    returns = [c.body for c in case.tracker.list_comments(1)
               if c.body.startswith("<!-- foragent:blocked")]
    assert len(returns) == 1 and FINDING in returns[0]


@pytest.mark.parametrize("reviewer", ["codex", "claude"])
@pytest.mark.parametrize("history", ["merge", "rebase"])
def test_a_new_own_commit_after_integration_is_reviewed_and_can_block(
    git_history, tmp_path, monkeypatch, history, reviewer,
):
    case = case_for(git_history, tmp_path, monkeypatch, reviewer=reviewer, review="careful")
    case.implementer.steps = [implement(content())]
    capture_review(case, [PASS])
    assert run_issue(1, case.ctx) == "pr-open"
    passed = case.tracker.pr_head_sha(2)
    integrated = integrate(case, history, passed)
    own = import_head(case, integrated, content(retry=2))
    assert git_command(case.history.origin, "rev-parse", f"{own}^") == integrated
    assert git_command(case.history.origin, "diff", "--name-only", integrated, own) == "feature.txt"
    restart(case)
    contexts = capture_review(case, ["VERDICT: FAIL\n" + FINDING])
    assert review_only(1, case.ctx) == "review-failed"
    assert len(contexts) == 1 and len(case.implementer.calls) == 1
    assert "+retry = 2" in contexts[0] and passed in contexts[0]
    assert "PASS-RECEIPT-47" in contexts[0]
    assert_main_absent(contexts[0])
    assert not case.tracker.merged_prs
    assert FINDING in case.ctx.store.log_path(1).read_text()
