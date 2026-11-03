"""Acceptance test for orch-merge-approved-inputs. Locked by Foragent; do not edit."""

import pytest

from conftest import ACCEPTANCE, git_command
from test_orch_merge import premerge_case
from test_skills_autosync import cli_exit


def assert_refused(case, main, pr, capsys, *reason_parts):
    """Exercise the public command; a refusal must leave the PR unmerged."""
    capsys.readouterr()
    code = cli_exit(main, ["orch", "merge", str(pr), "--approved-by", "jin"])
    output = capsys.readouterr()
    assert code != 0, f"Unsafe PR merged: {output.out}"
    assert case.tracker.merged_prs == []
    assert not any("Approved by" in comment.body for comment in case.tracker.list_comments(pr))
    lines = output.err.strip().splitlines()
    assert len(lines) == 1, f"Expected one precise refusal line: {output.err!r}"
    for part in reason_parts:
        assert part.casefold() in lines[0].casefold(), lines[0]


@pytest.mark.parametrize("ref", ["head", "base"])
@pytest.mark.parametrize("change", ["edit", "delete"])
def test_person_approved_merge_refuses_changed_locked_test_hashes(
    git_history, tmp_path, monkeypatch, capsys, ref, change,
):
    # The configured suite and CI pass. Even an edit that keeps the locked test
    # green must be refused because the person approved different bytes.
    case, main, pr, head = premerge_case(git_history, tmp_path, monkeypatch)
    base = case.history.tip
    original = git_command(case.history.origin, "show", f"{head}:{ACCEPTANCE}")
    case.history.tip = head if ref == "head" else base
    # GitHistory imports onto main; align that fixture ref with its parent
    # before constructing the alternate PR head (no forced history update).
    git_command(case.history.origin, "update-ref", "refs/heads/main", case.history.tip)
    case.history.update({ACCEPTANCE: original + "\n# Different approved bytes\n"
                         if change == "edit" else None})
    if ref == "head":
        head = case.history.tip
        git_command(case.history.origin, "update-ref", "refs/heads/foragent/issue-1", head)
        git_command(case.history.origin, "update-ref", "refs/heads/main", base)
        case.history.tip = base
        case.tracker.set_pr_head_sha(pr, head)
    case.git.fetch()
    # No validation helper is replaced: only offline Git and tracker fixtures.
    assert_refused(case, main, pr, capsys, "acceptance", ACCEPTANCE, ref)


def test_person_approved_merge_refuses_the_issues_failing_done_when(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.issue import body_hash

    case, main, pr, head = premerge_case(git_history, tmp_path, monkeypatch)
    command = "test -f release-ready"
    approval = case.store.load_approval(1)
    body = approval.body.replace("test -f feature.txt", command)
    issue = case.tracker.get_issue(1)
    case.tracker.update_issue(1, title=issue.title, body=body)
    case.store.save_approval(
        1, body=body, body_sha=body_hash(body), acceptance_blobs=approval.acceptance_blobs,
        approved_by=approval.approved_by, approved_at=approval.approved_at,
    )
    # This command fails on both the PR and base; an approved completion
    # condition is still mandatory even when the base already fails it.
    assert case.git.blob_sha(head, "release-ready") is None
    assert case.git.blob_sha("origin/main", "release-ready") is None
    assert_refused(case, main, pr, capsys, "Done when", command)
