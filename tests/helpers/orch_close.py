"""A superseded issue with its own lock, an open PR and a pushed branch (copied from the locked close test)."""

from conftest import ACCEPTANCE, git_command
from foragent.issue import body_hash
from helpers.home import cli_exit
from helpers.review_case import bind_cli, make_case


OWN = "tests/acceptance/test_issue_1_own.py"
BRANCH = "foragent/issue-1"
_WHY = "Its goal is now part of the merged base-refresh design."


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


def close(case, n=1, by=2, why=_WHY):
    return cli_exit(case.main, ["orch", "close", str(n), "--superseded-by", str(by), "--why", why])


def assert_nothing_changed(case):
    assert case.tracker.get_issue(1).state == "open"
    assert len(case.tracker.list_comments(1)) == case.before["comments"], "A refusal must not comment"
    assert case.tracker.get_issue(case.pr).state == "open", "A refusal must not close the PR"
    assert case.tracker.deleted_branches == [], "A refusal must not delete the branch"
    assert base_tip(case) == case.before["tip"] and base_files(case) == case.before["files"], (
        "A refusal must not change the base branch"
    )
