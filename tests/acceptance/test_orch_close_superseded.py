"""Acceptance test for orch-close-superseded: close an issue that another issue or PR replaced.

`fa orch close N --superseded-by M --why TEXT` is the one FA command for this. It:
- closes issue N with a comment that names #M and the reason;
- closes N's open Foragent PR and deletes that PR's branch;
- removes N's own locked acceptance files from the base branch, because N never
  merged. A locked file that a merged issue also approved stays.
It refuses an issue that is running and an issue that already merged, and then
changes nothing.

Real CLI, real state store, real Git history (a local bare origin); the tracker is
the project's fake tracker.
"""

from conftest import ACCEPTANCE, git_command
from foragent.issue import body_hash
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit

OWN = "tests/acceptance/test_issue_1_own.py"
BRANCH = "foragent/issue-1"
WHY = "Its goal is now part of the merged base-refresh design."


def base_files(case):
    return set(git_command(case.history.origin, "ls-tree", "-r", "--name-only", "refs/heads/main").splitlines())


def base_tip(case):
    return git_command(case.history.origin, "rev-parse", "refs/heads/main")


def approve(case, n, paths):
    body = case.tracker.get_issue(n).body
    case.store.save_approval(n, body=body, body_sha=body_hash(body),
                             acceptance_blobs=case.git.blob_shas("origin/main", paths),
                             approved_by="human", approved_at=case.clock[0].isoformat())


def superseded_case(git_history, tmp_path, monkeypatch, *, running=False):
    """#1 has its own locked file, a lock shared with merged #3, an open PR and a pushed branch.

    #2 is the issue that replaces it.
    """
    git_history.update({OWN: "def test_own():\n    assert False\n"}, message="Lock #1")
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    git_command(case.root, "fetch", "-q", "origin")
    body = case.tracker.get_issue(1).body
    approve(case, 1, [ACCEPTANCE, OWN])
    # #3 merged earlier and approved the same shared lock.
    case.tracker.add_issue(3, "Earlier merged work", body, labels=["approved"])
    approve(case, 3, [ACCEPTANCE])
    case.store.start_run(3, branch="foragent/issue-3", worktree=case.store.worktree_path(3))
    case.store.finish_run(3, outcome="merged")
    case.tracker.close_issue(3)
    # #2 replaces #1.
    case.tracker.add_issue(2, "The replacing design", body, labels=["approved"])
    # #1's work in progress: a pushed branch and its open Foragent PR.
    git_command(case.history.origin, "update-ref", f"refs/heads/{BRANCH}", base_tip(case))
    case.pr = case.tracker.create_pr(head=BRANCH, base="main", title="Implement feature (#1)", body="Closes #1")
    if not running:
        case.store.finish_run(1, outcome="retries-exceeded")
        case.tracker.remove_labels(1, ["running"])
        case.tracker.add_labels(1, ["blocked"])
    case.main = bind_cli(case, monkeypatch)
    monkeypatch.setattr("foragent.cli.RunContext", lambda **kwargs: case.ctx)
    case.before = {"files": base_files(case), "tip": base_tip(case),
                   "comments": len(case.tracker.list_comments(1))}
    return case


def close(case, n=1, by=2, why=WHY):
    return cli_exit(case.main, ["orch", "close", str(n), "--superseded-by", str(by), "--why", why])


def assert_nothing_changed(case):
    assert case.tracker.get_issue(1).state == "open"
    assert len(case.tracker.list_comments(1)) == case.before["comments"], "A refusal must not comment"
    assert case.tracker.get_issue(case.pr).state == "open", "A refusal must not close the PR"
    assert case.tracker.deleted_branches == [], "A refusal must not delete the branch"
    assert base_tip(case) == case.before["tip"] and base_files(case) == case.before["files"], (
        "A refusal must not change the base branch"
    )


def test_close_superseded_closes_issue_pr_branch_and_its_own_locks(git_history, tmp_path, monkeypatch):
    case = superseded_case(git_history, tmp_path, monkeypatch)

    assert close(case) == 0, "fa orch close --superseded-by must succeed for a blocked, unmerged issue"

    issue = case.tracker.get_issue(1)
    assert issue.state == "closed", "The superseded issue must be closed"
    added = case.tracker.list_comments(1)[case.before["comments"]:]
    assert len(added) == 1, f"Exactly one closing comment: {[c.body for c in added]}"
    assert "#2" in added[0].body and WHY in added[0].body, (
        f"The closing comment must name the replacing issue and the reason: {added[0].body!r}"
    )
    assert case.tracker.get_issue(case.pr).state == "closed", "The issue's open Foragent PR must be closed"
    assert case.tracker.deleted_branches == [BRANCH], (
        f"The PR's branch must be deleted exactly once: {case.tracker.deleted_branches}"
    )
    files = base_files(case)
    assert OWN not in files, "The never-merged issue's own locked file must leave the base branch"
    assert ACCEPTANCE in files, "A lock that a merged issue also approved must stay on the base branch"
    assert files == case.before["files"] - {OWN}, "Only the issue's own locked files may be removed"
    count = git_command(case.history.origin, "rev-list", "--count", f"{case.before['tip']}..refs/heads/main")
    assert count == "1", f"The removal is one commit on the base branch, not {count}"
    assert case.tracker.get_issue(2).state == "open", "The replacing issue is not touched"


def test_close_superseded_refuses_a_running_issue(git_history, tmp_path, monkeypatch, capsys):
    case = superseded_case(git_history, tmp_path, monkeypatch, running=True)
    capsys.readouterr()

    assert close(case) != 0, "A running issue must be stopped first, not closed under its runner"
    output = capsys.readouterr()
    assert "running" in (output.out + output.err).lower(), f"The refusal must say the issue is running: {output}"
    assert_nothing_changed(case)


def test_close_superseded_refuses_an_issue_that_already_merged(git_history, tmp_path, monkeypatch, capsys):
    case = superseded_case(git_history, tmp_path, monkeypatch)
    # #1 merged: its run merged, its issue is closed. Its locks are now the base's own tests.
    case.store.start_run(1, branch=BRANCH, worktree=case.store.worktree_path(1))
    case.store.finish_run(1, outcome="merged")
    case.tracker.close_issue(1)
    case.before["comments"] = len(case.tracker.list_comments(1))
    capsys.readouterr()

    assert close(case) != 0, "A merged issue is not superseded work; the command must refuse it"
    output = capsys.readouterr()
    assert "merged" in (output.out + output.err).lower(), f"The refusal must say the issue merged: {output}"
    assert len(case.tracker.list_comments(1)) == case.before["comments"]
    assert case.tracker.get_issue(case.pr).state == "open" and case.tracker.deleted_branches == []
    assert base_tip(case) == case.before["tip"] and OWN in base_files(case), (
        "A merged issue's locks belong to the base and must stay"
    )
