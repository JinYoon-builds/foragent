"""Acceptance test for resume-short-lock. Locked by Foragent; do not edit."""

from dataclasses import replace

import pytest

from conftest import git_command
from foragent.issue import body_hash
from test_base_move_reuse import publish_case  # noqa: F401
from test_short_merge_lock import (
    assert_merged, assert_short_holds, run, short_case, slow_under_lock,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


BRANCH = "foragent/issue-1"
RESOLVED = "theirs\nmine\n"


def saved_run(case, *, saved_conflict=True):
    """Save real WIP, then restart it through run_issue's normal resume path.

    Use conflict-budget's real GitHistory add/add conflict and scripted
    implementer pattern. A product question stops the first execution without
    signals; the runner itself saves markers and merge_conflicts for restart.
    The other variant saves clean WIP before the target moves.
    """
    case.ctx.poll_hook = lambda execute, poll: execute()

    def ask(kwargs):
        question = kwargs["worktree"] / ".foragent/QUESTION.md"
        question.parent.mkdir(exist_ok=True)
        question.write_text("Keep both versions? A: yes (recommended); B: no.\n")
        return "Saved work pending a product decision"

    def first(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("mine\n")
        (kwargs["worktree"] / "kept-work.txt").write_text("keep saved work\n")
        if saved_conflict:
            case.history.update({"feature.txt": "theirs\n"})
            return "Implemented"
        return ask(kwargs)

    def stop_in_conflict(kwargs):
        assert case.store.get_run(1).phase == "resolving-conflict"
        assert "<<<<<<<" in (kwargs["worktree"] / "feature.txt").read_text()
        return ask(kwargs)

    case.implementer.steps = [first, stop_in_conflict] if saved_conflict else [first]
    assert run(case) == "spec-question", case.store.get_run(1).detail
    assert case.store.get_run(1).merge_conflicts == (["feature.txt"] if saved_conflict else [])
    if not saved_conflict:
        case.history.update({"feature.txt": "theirs\n"})
    case.resume_base = case.history.tip
    case.saved_head = case.git.local_branch_sha(BRANCH)
    assert case.saved_head is not None
    assert git_command(case.root, "show", f"{BRANCH}:kept-work.txt") == "keep saved work"
    assert case.merges == []
    assert_short_holds(case)

    case.advance(60)
    case.tracker.remove_labels(1, ["blocked"])
    case.tracker.add_labels(1, ["running"])
    case.store.start_run(1, branch=BRANCH, worktree=case.store.worktree_path(1))
    assert case.store.get_run(1).merge_conflicts == (["feature.txt"] if saved_conflict else [])
    case.implementer.calls.clear()
    case.reviewer.calls.clear()
    case.reviewer.steps = [case.review]
    case.events.clear()
    case.holds.clear()
    case.verifications.clear()
    return case


def resolve(case, kwargs):
    root = kwargs["worktree"]
    assert case.store.get_run(1).phase == "resolving-conflict"
    assert "<<<<<<<" in (root / "feature.txt").read_text()
    assert (root / "kept-work.txt").read_text() == "keep saved work\n"
    # The shared fake sleep records model time without any wall-clock wait.
    case.ctx.sleep(5)
    (root / "feature.txt").write_text(RESOLVED)
    return "Resolved both versions in place"


@pytest.mark.parametrize("saved_conflict", [True, False], ids=["saved-conflict", "moved-target"])
def test_resumed_conflict_resolves_and_verifies_outside_merge_lock(short_case, saved_conflict):
    case = saved_run(short_case, saved_conflict=saved_conflict)
    case.implementer.steps = [lambda kwargs: resolve(case, kwargs)]

    assert run(case) == "merged", case.store.get_run(1).detail
    assert_merged(case)
    assert len(case.implementer.calls) == len(case.reviewer.calls) == 1
    assert case.verifications == [case.resume_base], "Resume must run the real verification suite"
    assert case.store.get_run(1).merge_conflicts == []
    assert case.store.get_run(1).attempts == 1, "Successful resolution must not rebuild"
    assert git_command(case.history.origin, "show", "main:feature.txt") == RESOLVED.strip()
    assert git_command(case.history.origin, "show", "main:kept-work.txt") == "keep saved work"
    git_command(case.history.origin, "merge-base", "--is-ancestor", case.saved_head, "main")
    git_command(case.history.origin, "merge-base", "--is-ancestor", case.resume_base, "main")
    assert_short_holds(case)


def test_ready_run_merges_while_resumed_conflict_is_being_resolved(short_case):
    case = saved_run(short_case)
    approval = case.store.load_approval(1)
    body = approval.body.replace("feature.txt", "feature-b.txt")
    case.tracker.add_issue(4, "Independent feature", body, labels=["ready", "approved"])
    case.tracker.record_label(4, "approved", "human")
    case.store.save_approval(
        4, body=body, body_sha=body_hash(body), acceptance_blobs=approval.acceptance_blobs,
        approved_by="human", approved_at=case.clock[0].isoformat(),
    )
    case.store.start_run(4, branch="foragent/issue-4", worktree=case.store.worktree_path(4))
    contender = []

    def resolve_a(kwargs):
        assert case.store.get_run(1).phase == "resolving-conflict"
        assert case.merges == []
        # Cooperatively run B at the fake implementer boundary. observed_lock
        # rejects a nested acquisition immediately, so old code cannot deadlock.
        contender.append(run(case, 4))
        return resolve(case, kwargs)

    def implement_b(kwargs):
        (kwargs["worktree"] / "feature-b.txt").write_text("independent\n")
        return "Implemented B"

    case.implementer.steps = [resolve_a, implement_b]
    case.reviewer.steps = [case.review, case.review]
    outcome = run(case)

    assert contender == ["merged"], (
        "Ready B must merge during A's resumed resolution: " + case.store.get_run(4).detail
    )
    assert outcome == "merged", case.store.get_run(1).detail
    assert_merged(case, 4)
    assert_merged(case)
    assert [row.issue for row in case.merges] == [4, 1]
    verification = next(row for row in case.events if row.kind == "verify" and row.issue == 1)
    assert case.merges[0].at < verification.at, "B must finish before A's resumed verification"
    assert git_command(case.history.origin, "show", "main:feature-b.txt") == "independent"
    assert git_command(case.history.origin, "show", "main:feature.txt") == RESOLVED.strip()
    assert_short_holds(case)


def test_exhausted_resume_conflict_budget_still_rebuilds_without_slow_work_under_lock(short_case):
    case = saved_run(short_case)
    case.ctx.config = replace(case.ctx.config, conflict_retries=1)
    turns = []

    def give_up(kwargs):
        assert case.store.get_run(1).phase == "resolving-conflict"
        assert "<<<<<<<" in (kwargs["worktree"] / "feature.txt").read_text()
        turns.append("conflict")
        case.ctx.sleep(5)
        return "Could not resolve"

    def rebuild(kwargs):
        root = kwargs["worktree"]
        assert case.store.get_run(1).phase == "implementing"
        assert git_command(root, "rev-parse", "HEAD") == case.resume_base
        assert (root / "feature.txt").read_text() == "theirs\n"
        assert case.store.get_run(1).merge_conflicts == []
        turns.append("rebuild")
        (root / "feature.txt").write_text(RESOLVED)
        return "Re-implemented on the latest base"

    case.implementer.steps = [give_up, rebuild]
    assert run(case) == "merged", case.store.get_run(1).detail
    assert turns == ["conflict", "rebuild"], "One conflict attempt, then one clean rebuild"
    assert len(case.implementer.calls) == 2
    assert case.store.get_run(1).attempts == 2
    assert case.verifications == [case.resume_base]
    assert_merged(case)
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    assert git_command(case.history.origin, "show", "main:feature.txt") == RESOLVED.strip()
    assert not slow_under_lock(case), "Resumed conflict-budget exhaustion waited under merge.lock"
    assert_short_holds(case)
