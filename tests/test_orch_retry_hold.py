"""Boundaries of the same-class retry refusal: contents decide, never approval times."""

from types import SimpleNamespace

from foragent import orch_retry_hold
from foragent.git import GitError
from foragent.reasons import Reason
from foragent.state_records import Flag

HELD = "2026-11-07T10:00:00+00:00"
LATER = "2026-11-07T11:00:00+00:00"  # Every approval below is newer than the hold.
LOCK = "tests/acceptance/test_feature.py"


def context(*, reason=Reason.REVIEW_DEFECTS, run_sha="body-1", body_sha="body-1",
            held_blob="blob-1", approved_blob="blob-1", git_fails=False):
    calls = []

    def call(args):
        calls.append(args)
        if git_fails:
            raise GitError("not a git repository")
        return "base-at-hold\n"

    def blob_shas(ref, paths):
        assert ref == "base-at-hold"
        return {path: held_blob for path in paths}

    approval = SimpleNamespace(body_sha=body_sha, approved_at=LATER, acceptance_blobs={LOCK: approved_blob})
    store = SimpleNamespace(
        load_flag=lambda n: Flag(n, reason, HELD) if reason else None,
        load_approval=lambda n: approval,
        get_run=lambda n: SimpleNamespace(approval_body_sha=run_sha),
    )
    git = SimpleNamespace(_call=call, blob_shas=blob_shas)
    return SimpleNamespace(store=store, git=git, config=SimpleNamespace(base_branch="main")), calls


def test_reapproval_without_any_change_is_still_refused():
    ctx, _ = context()
    text = orch_retry_hold.refusal(ctx, 7)
    assert text.startswith("#7 ") and "order edit" in text


def test_approved_goal_edit_allows_retry():
    ctx, _ = context(body_sha="body-2")
    assert orch_retry_hold.refusal(ctx, 7) is None


def test_approved_acceptance_test_edit_allows_retry():
    ctx, calls = context(approved_blob="blob-2")
    assert orch_retry_hold.refusal(ctx, 7) is None
    # The locked test contents are read on the base as it was when the hold was flagged.
    assert calls == [["rev-list", "-1", "--first-parent", "--until=1794045600", "origin/main"]]


def test_unknown_hold_contents_keep_the_refusal():
    ctx, _ = context(approved_blob="blob-2", git_fails=True)
    assert orch_retry_hold.refusal(ctx, 7) is not None
    ctx, _ = context(run_sha=None)
    assert orch_retry_hold.refusal(ctx, 7) is not None


def test_other_holds_and_unheld_issues_are_not_refused():
    ctx, _ = context(reason=Reason.REVIEW_DISPUTE)
    assert orch_retry_hold.refusal(ctx, 7) is None
    ctx, _ = context(reason=None)
    assert orch_retry_hold.refusal(ctx, 7) is None
